<#
.SYNOPSIS
  Install Docker Engine inside WSL 2 on a Windows host (client or Server 2022+).

.DESCRIPTION
  Idempotent. Safe to re-run.

  What it does:
    1. checks admin + OS build
    2. enables the WSL and VirtualMachinePlatform features (may need one reboot)
    3. sets WSL default version to 2
    4. installs a Linux distribution if missing (default Ubuntu-22.04)
    5. installs docker-ce + compose plugin inside that distribution
    6. writes /etc/docker/daemon.json (log rotation) and /etc/wsl.conf (systemd)
    7. writes %USERPROFILE%\.wslconfig (memory / cpu caps)
    8. verifies with docker run --rm hello-world

  Exit codes:
    0    = done
    3010 = features enabled, reboot required, then run this again

  Notes:
    - Docker Desktop is a different product and is NOT supported on Windows
      Server. Do not install Desktop on a server; this script does not.
    - Needs outbound HTTPS to download.docker.com / get.docker.com. For offline
      hosts see "Offline install" in README.md.
    - By default the distribution is registered for the account running this
      script. The autostart task must run as that same account (see
      register-autostart.ps1).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-docker-wsl.ps1
  powershell -ExecutionPolicy Bypass -File .\install-docker-wsl.ps1 -Mirror Aliyun
#>
param(
  [string]$Distro = "Ubuntu-22.04",
  [string]$Mirror = "",
  [int]$MemoryGB = 8,
  [int]$Processors = 0,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$m) { Write-Host ""; Write-Host ("== " + $m) -ForegroundColor Cyan }
function Write-Ok([string]$m) { Write-Host ("   [ok] " + $m) -ForegroundColor Green }
function Write-Warn2([string]$m) { Write-Host ("   [warn] " + $m) -ForegroundColor Yellow }
function Write-Err2([string]$m) { Write-Host ("   [fail] " + $m) -ForegroundColor Red }

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err2 "must run as Administrator (right-click PowerShell -> Run as administrator)"
    exit 1
  }
}

function ConvertTo-WslPath([string]$winPath) {
  # D:\Forge\deploy -> /mnt/d/Forge/deploy
  $p = $winPath -replace "\\", "/"
  if ($p -match "^([A-Za-z]):(.*)$") {
    $drive = $matches[1].ToLower()
    $rest = $matches[2]
    return "/mnt/$drive$rest"
  }
  return $p
}

function Invoke-Native([string]$exe, [string[]]$NativeArgs) {
  # Under $ErrorActionPreference=Stop a missing or policy-blocked executable
  # raises NativeCommandFailed instead of returning a useful message. Always
  # capture it so the operator sees what actually happened.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & $exe @NativeArgs 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
  } catch {
    return @{ Code = 9009; Out = $_.Exception.Message }
  } finally {
    $ErrorActionPreference = $prev
  }
}

function Invoke-Wsl([string]$distro, [string[]]$wslArgs, [switch]$AllowFail) {
  $r = Invoke-Native "wsl.exe" (@("-d", $distro) + $wslArgs)
  if ($r.Code -ne 0 -and -not $AllowFail) {
    throw ("wsl " + ($wslArgs -join " ") + " failed (exit " + $r.Code + "): " + $r.Out.Trim())
  }
  return $r
}

Write-Host "============================================================"
Write-Host " FORGE - install Docker Engine inside WSL 2"
Write-Host "============================================================"

Assert-Admin

# ---------------------------------------------------------------- 1. OS check
Write-Step "1/8 checking OS"
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
$isServer = $os.Caption -match "Server"
Write-Ok ("{0} (build {1})" -f $os.Caption, $build)
if ($isServer -and $build -lt 20348) {
  Write-Err2 "Windows Server 2019 only has WSL 1 - cannot run Linux containers. Need Server 2022+."
  exit 1
}
if (-not $isServer -and $build -lt 19041) {
  Write-Err2 "Windows build $build is too old (need 19041+)."
  exit 1
}
$cs = Get-CimInstance Win32_ComputerSystem
if (-not $cs.HypervisorPresent) {
  Write-Warn2 "HypervisorPresent=False - WSL 2 may fail to start."
  Write-Warn2 "On a cloud VM, ask the provider for nested virtualization."
}

# ---------------------------------------------------------------- 2. features
Write-Step "2/8 enabling WSL + VirtualMachinePlatform"
$needReboot = $false
foreach ($f in "Microsoft-Windows-Subsystem-Linux", "VirtualMachinePlatform") {
  $state = "unknown"
  try {
    $feat = Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction Stop
    $state = $feat.State
  } catch {
    Write-Warn2 ("cannot query " + $f + ": " + $_.Exception.Message)
  }
  if ($state -eq "Enabled") {
    Write-Ok ($f + " already enabled")
  } else {
    Write-Host ("   enabling " + $f + " ...")
    $r = & dism.exe /online /enable-feature /featurename:$f /all /norestart 2>&1 | Out-String
    if ($LASTEXITCODE -eq 3010) { $needReboot = $true }
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {
      Write-Err2 ("dism failed for " + $f + ": " + $r.Trim())
      exit 1
    }
    Write-Ok ($f + " enabled (reboot pending)")
    $needReboot = $true
  }
}

if ($needReboot -and -not $Force) {
  Write-Host ""
  Write-Warn2 "A reboot is required before WSL 2 can start."
  Write-Host "   Reboot now, then run this script again:"
  Write-Host "     shutdown /r /t 0"
  Write-Host "   If you are certain the features are already active, re-run with -Force."
  exit 3010
}

# ---------------------------------------------------------------- 3. default v2
Write-Step "3/8 setting WSL default version to 2"
$dv = Invoke-Native "wsl.exe" @("--set-default-version", "2")
if ($dv.Code -ne 0) {
  Write-Err2 ("wsl --set-default-version 2 failed: " + $dv.Out.Trim())
  Write-Host "   if wsl.exe is missing entirely, enable the feature and reboot first:"
  Write-Host "     dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all"
  exit 1
}
Write-Ok "default version = 2"

# make sure the WSL kernel is current (older Server 2022 images ship an old one)
$upd = Invoke-Native "wsl.exe" @("--update")
if ($upd.Code -eq 0) { Write-Ok "wsl kernel updated" } else { Write-Warn2 ("wsl --update: " + $upd.Out.Trim()) }

# ---------------------------------------------------------------- 4. distro
Write-Step ("4/8 ensuring distribution " + $Distro)
$listRes = Invoke-Native "wsl.exe" @("-l", "-q")
$listText = $listRes.Out
$installed = $false
foreach ($ln in ($listText -split "`r?`n")) {
  if ($ln.Trim() -and $ln.Trim().ToLower().StartsWith($Distro.ToLower())) { $installed = $true }
}
if ($installed) {
  Write-Ok ($Distro + " already installed")
} else {
  Write-Host ("   installing " + $Distro + " (downloads from Microsoft, needs internet) ...")
  $r = Invoke-Native "wsl.exe" @("--install", "-d", $Distro, "--no-launch", "--web-download")
  if ($r.Code -ne 0) {
    Write-Warn2 ("--web-download failed, retrying without it: " + $r.Out.Trim())
    $r2 = Invoke-Native "wsl.exe" @("--install", "-d", $Distro, "--no-launch")
    if ($r2.Code -ne 0) {
      Write-Err2 ("could not install " + $Distro + ": " + $r2.Out.Trim())
      Write-Host "   On Windows Server without Store access, download the .appx and run:"
      Write-Host "     Add-AppxPackage .\Ubuntu_2204.appx"
      exit 1
    }
  }
  Write-Ok ($Distro + " installed")
}

# first run: initialise as root so no interactive user creation blocks us
$probe = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "/bin/true") -AllowFail
if ($probe.Code -ne 0) {
  Write-Err2 ("distribution does not start: " + $probe.Out.Trim())
  Write-Host "   Most common cause: nested virtualization unavailable (cloud VM without VT passthrough)."
  exit 1
}
Write-Ok "distribution starts"

# ---------------------------------------------------------------- 5. docker
Write-Step "5/8 installing docker-ce + compose plugin"
$ver = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c", "command -v docker || true")
$hasDocker = $ver.Out.Trim().Length -gt 0
if ($hasDocker) {
  Write-Ok "docker already present"
} else {
  $installCmd = "curl -fsSL https://get.docker.com | sh"
  if ($Mirror) { $installCmd = "curl -fsSL https://get.docker.com | sh -s -- --mirror $Mirror" }
  Write-Host ("   running: " + $installCmd)
  $r = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "bash", "-lc", $installCmd) -AllowFail
  if ($r.Code -ne 0) {
    Write-Err2 ("docker install failed: " + $r.Out.Trim())
    Write-Host "   If this host has no internet, use the offline route in README.md."
    exit 1
  }
  Write-Ok "docker installed"
}

# ---------------------------------------------------------------- 6. daemon.json
Write-Step "6/8 writing daemon.json (log rotation) and wsl.conf (systemd)"
$tmpDir = Join-Path $env:TEMP "forge-wsl"
if (-not (Test-Path $tmpDir)) { New-Item -ItemType Directory -Path $tmpDir | Out-Null }
$daemonLocal = Join-Path $tmpDir "daemon.json"
$daemonJson = @'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" },
  "max-concurrent-downloads": 4
}
'@
Set-Content -Path $daemonLocal -Value $daemonJson -Encoding ASCII
$daemonWsl = ConvertTo-WslPath $daemonLocal
$cp = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c",
  "mkdir -p /etc/docker && cp $daemonWsl /etc/docker/daemon.json && cat /etc/docker/daemon.json") -AllowFail
if ($cp.Code -eq 0) { Write-Ok "daemon.json written" } else { Write-Warn2 ("daemon.json: " + $cp.Out.Trim()) }

$wslConfLocal = Join-Path $tmpDir "wsl.conf"
Set-Content -Path $wslConfLocal -Value "[boot]`nsystemd=true`n" -Encoding ASCII
$wslConfWsl = ConvertTo-WslPath $wslConfLocal
$cp2 = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c",
  "cp $wslConfWsl /etc/wsl.conf && cat /etc/wsl.conf") -AllowFail
if ($cp2.Code -eq 0) { Write-Ok "wsl.conf written (systemd=true)" } else { Write-Warn2 ("wsl.conf: " + $cp2.Out.Trim()) }

# ---------------------------------------------------------------- 7. .wslconfig
Write-Step "7/8 writing .wslconfig (memory / cpu caps)"
$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$cpuCount = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
if ($Processors -le 0) {
  $Processors = [math]::Max(2, [math]::Min(8, $cpuCount - 2))
}
$totalGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($MemoryGB -gt ($totalGB - 4)) { $MemoryGB = [math]::Max(4, $totalGB - 4) }
$wslConfig = @"
[wsl2]
memory=${MemoryGB}GB
processors=$Processors
swap=2GB
localhostForwarding=true
"@
if (Test-Path $wslConfigPath) {
  $bak = $wslConfigPath + ".bak"
  Copy-Item $wslConfigPath $bak -Force
  Write-Warn2 ("existing .wslconfig backed up to " + $bak)
}
Set-Content -Path $wslConfigPath -Value $wslConfig -Encoding ASCII
Write-Ok ("memory=" + $MemoryGB + "GB processors=" + $Processors + " (" + $cpuCount + " logical cpus total)")

# restart the distribution so systemd + daemon.json take effect
[void](Invoke-Native "wsl.exe" @("--shutdown"))
Start-Sleep -Seconds 3

# ---------------------------------------------------------------- 8. verify
Write-Step "8/8 verifying"
$dockerVer = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c", "docker version --format '{{.Server.Version}}'") -AllowFail
if ($dockerVer.Code -ne 0 -or -not $dockerVer.Out.Trim()) {
  Write-Warn2 ("docker daemon not up yet: " + $dockerVer.Out.Trim())
  Write-Host "   trying: service docker start"
  Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c", "service docker start || systemctl start docker") -AllowFail | Out-Null
  Start-Sleep -Seconds 5
  $dockerVer = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c", "docker version --format '{{.Server.Version}}'") -AllowFail
}
if ($dockerVer.Code -eq 0 -and $dockerVer.Out.Trim()) {
  Write-Ok ("docker server version " + $dockerVer.Out.Trim())
} else {
  Write-Err2 "docker daemon still not reachable"
  Write-Host "   run inside WSL: sudo service docker start ; journalctl -u docker -n 50"
  exit 1
}

$composeVer = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "sh", "-c", "docker compose version") -AllowFail
if ($composeVer.Code -eq 0) { Write-Ok ("compose plugin " + $composeVer.Out.Trim()) } else { Write-Warn2 "compose plugin missing" }

$hello = Invoke-Wsl -distro $Distro -wslArgs @("-u", "root", "--", "docker", "run", "--rm", "hello-world") -AllowFail
if ($hello.Code -eq 0) { Write-Ok "hello-world container ran" } else { Write-Warn2 ("hello-world failed: " + $hello.Out.Trim()) }

Write-Host ""
Write-Host "============================================================"
Write-Host " DONE"
Write-Host " Next:"
Write-Host "   1. unzip the deploy package to D:\Forge\deploy"
Write-Host "   2. copy .env.example to .env and fill DOMAIN / ACME_EMAIL / passwords"
Write-Host "   3. .\forge-stack.ps1 -Action start -StackDir D:\Forge\deploy"
Write-Host "   4. .\register-autostart.ps1 -StackDir D:\Forge\deploy"
Write-Host "============================================================"
exit 0
