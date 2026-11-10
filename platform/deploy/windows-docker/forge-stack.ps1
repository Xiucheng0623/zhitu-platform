<#
.SYNOPSIS
  Start / stop / inspect the FORGE docker stack that lives inside WSL 2.

.DESCRIPTION
  Thin wrapper: every action runs `docker compose` inside the WSL distribution,
  in the directory where docker-compose.yml lives on the Windows side.

  This script does not require Administrator unless the WSL distribution has not
  been started yet (the first `wsl` call may need it on some builds).

  Why a wrapper instead of running docker from PowerShell:
    docker.exe on the PATH only exists if Docker Desktop is installed. On Windows
    Server we install Docker Engine inside WSL instead, so the CLI must be invoked
    through `wsl -d <distro>`. Keeping that in one place avoids every operator
    inventing their own quoting.

.EXAMPLE
  .\forge-stack.ps1 -Action start
  .\forge-stack.ps1 -Action status
  .\forge-stack.ps1 -Action logs -Service orchestrator -Tail 200
  .\forge-stack.ps1 -Action backup
#>
param(
  [ValidateSet("start", "stop", "restart", "status", "ps", "logs", "pull",
               "update", "backup", "smoke", "shell", "env")]
  [string]$Action = "status",
  [string]$Distro = "Ubuntu-22.04",
  [string]$StackDir = "D:\Forge\deploy",
  [string]$Service = "",
  [int]$Tail = 120,
  [switch]$NoPortProxy,
  [int[]]$Ports = @(80, 443)
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$m) { Write-Host ""; Write-Host ("== " + $m) -ForegroundColor Cyan }
function Write-Ok([string]$m) { Write-Host ("   [ok] " + $m) -ForegroundColor Green }
function Write-Warn2([string]$m) { Write-Host ("   [warn] " + $m) -ForegroundColor Yellow }
function Write-Err2([string]$m) { Write-Host ("   [fail] " + $m) -ForegroundColor Red }

function ConvertTo-WslPath([string]$winPath) {
  $p = $winPath -replace "\\", "/"
  if ($p -match "^([A-Za-z]):(.*)$") {
    return "/mnt/" + $matches[1].ToLower() + $matches[2]
  }
  return $p
}

# Native commands do not "throw" in a useful way under $ErrorActionPreference=Stop:
# a missing or policy-blocked wsl.exe surfaces as NativeCommandFailed with the
# PowerShell error record, not the tool's own message. Wrap every call so the
# operator sees "cannot start WSL" instead of a stack trace.
function Invoke-WslRaw {
  param([string[]]$WslArgs)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $out = & wsl.exe @WslArgs 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Out = $out }
  } catch {
    return @{ Code = 9009; Out = $_.Exception.Message }
  } finally {
    $ErrorActionPreference = $prev
  }
}

if (-not (Test-Path (Join-Path $StackDir "docker-compose.yml"))) {
  Write-Err2 ("no docker-compose.yml in " + $StackDir)
  Write-Host "   unzip the deploy package first, then pass -StackDir <that folder>"
  exit 1
}

$wslDir = ConvertTo-WslPath $StackDir

# make sure the distribution is up and the daemon is running; a Windows reboot
# leaves WSL stopped, and every action below would fail with a confusing error.
$probe = Invoke-WslRaw @("-d", $Distro, "-u", "root", "--", "/bin/true")
if ($probe.Code -ne 0) {
  Write-Err2 ("cannot start WSL distribution " + $Distro)
  Write-Host ("   detail: " + $probe.Out.Trim())
  Write-Host "   check:  wsl -l -v   /   wsl --update"
  Write-Host "   if the message is about access denied, wsl.exe is blocked by policy on this host"
  Write-Host "   if the message says distribution not found, it is registered for another account"
  Write-Host "   and this task/session cannot see it"
  exit 1
}

$info = Invoke-WslRaw @("-d", $Distro, "-u", "root", "--", "sh", "-c", "docker info >/dev/null 2>&1 && echo UP || echo DOWN")
if ($info.Out.Trim() -ne "UP") {
  Write-Warn2 "docker daemon is down, starting it"
  [void](Invoke-WslRaw @("-d", $Distro, "-u", "root", "--", "sh", "-c", "service docker start >/dev/null 2>&1 || systemctl start docker"))
  Start-Sleep -Seconds 4
  $info2 = Invoke-WslRaw @("-d", $Distro, "-u", "root", "--", "sh", "-c", "docker info >/dev/null 2>&1 && echo UP || echo DOWN")
  if ($info2.Out.Trim() -ne "UP") {
    Write-Err2 "docker daemon still down"
    Write-Host "   inside WSL: sudo service docker start ; sudo journalctl -u docker -n 80"
    exit 1
  }
}

function Run-InStack([string]$cmd) {
  Write-Host ("   $ " + $cmd) -ForegroundColor DarkGray
  $full = "cd " + $wslDir + " && " + $cmd
  $r = Invoke-WslRaw @("-d", $Distro, "-u", "root", "--", "bash", "-lc", $full)
  if ($r.Out.Trim()) { Write-Host $r.Out.TrimEnd() }
  return $r.Code
}

switch ($Action) {
  "start" {
    Write-Step "starting stack"
    $code = Run-InStack "docker compose up -d"
    if ($code -ne 0) { Write-Err2 "compose up failed"; exit $code }
    Write-Ok "started"
    Run-InStack "docker compose ps" | Out-Null
    # The WSL VM gets a new IP on every reboot, so a portproxy rule written last
    # week points at nothing today. Refresh it now, while we know WSL is up.
    if (-not $NoPortProxy) {
      $pp = Join-Path $PSScriptRoot "sync-portproxy.ps1"
      if (Test-Path $pp) {
        Write-Step "publishing ports on the Windows LAN interface"
        & $pp -Distro $Distro -Ports $Ports | Out-Null
      }
    }
  }
  "stop" {
    Write-Step "stopping stack (containers only, volumes kept)"
    $code = Run-InStack "docker compose stop"
    if ($code -ne 0) { Write-Err2 "compose stop failed"; exit $code }
    Write-Ok "stopped"
  }
  "restart" {
    Write-Step "restarting stack"
    $code = Run-InStack "docker compose restart"
    if ($code -ne 0) { Write-Err2 "compose restart failed"; exit $code }
    Write-Ok "restarted"
  }
  "status" {
    Write-Step "container status"
    Run-InStack "docker compose ps" | Out-Null
    Write-Step "host reachability"
    foreach ($u in "http://localhost/", "http://localhost:8100/health") {
      try {
        $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 6
        Write-Ok ($u + " -> " + $r.StatusCode)
      } catch {
        Write-Warn2 ($u + " -> " + $_.Exception.Message)
      }
    }
  }
  "ps" {
    Run-InStack "docker compose ps" | Out-Null
  }
  "logs" {
    $cmd = "docker compose logs --tail=$Tail"
    if ($Service) { $cmd = $cmd + " " + $Service }
    Run-InStack $cmd | Out-Null
  }
  "pull" {
    Write-Step "pulling images"
    $code = Run-InStack "docker compose pull"
    if ($code -ne 0) { Write-Warn2 "some images could not be pulled (offline?)" }
    else { Write-Ok "images up to date" }
  }
  "update" {
    Write-Step "updated code + rebuild"
    $code = Run-InStack "docker compose up -d --build"
    if ($code -ne 0) { Write-Err2 "update failed"; exit $code }
    Write-Ok "updated"
    Run-InStack "docker compose ps" | Out-Null
  }
  "backup" {
    Write-Step "running backup.sh inside WSL"
    # bash is explicit: Windows filesystems do not keep the +x bit
    Run-InStack "bash ./backup.sh" | Out-Null
  }
  "smoke" {
    Write-Step "running smoke.sh inside WSL"
    Run-InStack "bash ./smoke.sh" | Out-Null
  }
  "env" {
    Write-Step "effective env (secrets masked)"
    $code = Run-InStack "grep -E '^[A-Z_]+=' .env | sed -E 's/(SECRET|PASSWORD|TOKEN)=.*/\\1=***/'"
    if ($code -ne 0) { exit $code }
  }
  "shell" {
    Write-Host ("entering " + $Distro + " as root, cd to " + $wslDir)
    & wsl.exe -d $Distro -u root --cd $wslDir
  }
}
