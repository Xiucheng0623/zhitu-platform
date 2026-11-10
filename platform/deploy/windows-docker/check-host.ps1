<#
.SYNOPSIS
  Decide whether this Windows host can run the FORGE stack as Linux containers.

.DESCRIPTION
  Read-only. Changes nothing. Run this FIRST on the target server.

  Why this exists:
    Docker Desktop is NOT supported on Windows Server (Docker's own docs say so,
    and the subscription terms limit Desktop to client machines). On a server the
    only way to run Linux containers is WSL 2 with Docker Engine installed inside
    the distribution. WSL 2 needs nested virtualization. Most cheap cloud Windows
    VMs do not expose it, so "just install Docker" fails halfway and leaves a
    half-configured host. This script answers the question before anything is
    installed.

  Exit codes:
    0 = docker already usable        -> go straight to forge-stack.ps1
    2 = not installed, but installable -> run install-docker-wsl.ps1
    1 = blocked                       -> use deploy\windows\install.ps1 (bare metal)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\check-host.ps1
  powershell -ExecutionPolicy Bypass -File .\check-host.ps1 -OutFile D:\forge-host.txt
#>
param(
  [string]$OutFile = ""
)

$ErrorActionPreference = "Continue"
$lines = New-Object System.Collections.ArrayList

function Add-Line([string]$text) {
  [void]$lines.Add($text)
  Write-Host $text
}

function Add-Item([string]$label, [string]$value, [string]$note) {
  $msg = "  {0,-26} {1}" -f $label, $value
  if ($note) { $msg = $msg + "   ($note)" }
  Add-Line $msg
}

Add-Line "============================================================"
Add-Line " FORGE host check - can this machine run Linux containers?"
Add-Line (" time: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line "============================================================"
Add-Line ""

# ---------------------------------------------------------------- OS
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$build = 0
$caption = "unknown"
if ($os) {
  $build = [int]$os.BuildNumber
  $caption = $os.Caption
}
$isServer = $caption -match "Server"

Add-Line "[1] Operating system"
Add-Item "caption" $caption
Add-Item "build" "$build"
Add-Item "edition type" $(if ($isServer) { "Windows Server" } else { "client (Win10/11)" })

# ---------------------------------------------------------------- virtualization
$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
$hyper = $false
if ($cs) { $hyper = [bool]$cs.HypervisorPresent }
$cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
$vmFirmware = $null
$cpuName = "unknown"
if ($cpu) {
  $cpuName = $cpu.Name
  if ($null -ne $cpu.VirtualizationFirmwareEnabled) { $vmFirmware = [bool]$cpu.VirtualizationFirmwareEnabled }
}
$ramGB = 0
if ($cs) { $ramGB = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1) }

Add-Line ""
Add-Line "[2] Virtualization (WSL 2 needs this)"
Add-Item "cpu" $cpuName
Add-Item "hypervisor present" "$hyper"
if ($null -ne $vmFirmware) { Add-Item "vmx/svm in firmware" "$vmFirmware" } else { Add-Item "vmx/svm in firmware" "n/a (already hypervised)" }
Add-Item "physical memory" "$ramGB GB"

# ---------------------------------------------------------------- wsl
$wslExe = Get-Command wsl.exe -ErrorAction SilentlyContinue
$wslText = ""
$wslUsable = $false
if ($wslExe) {
  try {
    $wslText = (& wsl.exe -l -v 2>&1 | Out-String)
  } catch {
    $wslText = "ERR: " + $_.Exception.Message
  }
  if ($wslText -match "Ubuntu|Debian|kali|VERSION") { $wslUsable = $true }
}
$featWsl = "unknown"
$featVmp = "unknown"
try {
  $f1 = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -ErrorAction Stop
  $featWsl = $f1.State
} catch { }
try {
  $f2 = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction Stop
  $featVmp = $f2.State
} catch { }

Add-Line ""
Add-Line "[3] WSL"
Add-Item "wsl.exe" $(if ($wslExe) { $wslExe.Source } else { "NOT FOUND" })
Add-Item "WSL feature" $featWsl
Add-Item "VirtualMachinePlatform" $featVmp
if ($wslText.Trim()) {
  Add-Line "  --- wsl -l -v ---"
  foreach ($ln in ($wslText -split "`r?`n")) {
    if ($ln.Trim()) { Add-Line ("      " + $ln.Trim()) }
  }
}

# ---------------------------------------------------------------- docker
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
$dockerVersion = ""
$dockerWorks = $false
if ($dockerCmd) {
  try {
    $dockerVersion = (& docker version --format "{{.Server.Version}}" 2>&1 | Out-String).Trim()
    if ($dockerVersion -and $dockerVersion -notmatch "error|Cannot|refused") { $dockerWorks = $true }
  } catch { }
}

Add-Line ""
Add-Line "[4] Docker"
Add-Item "docker.exe" $(if ($dockerCmd) { $dockerCmd.Source } else { "NOT FOUND" })
if ($dockerCmd) { Add-Item "daemon reachable" "$dockerWorks" $dockerVersion }

# ---------------------------------------------------------------- ports
Add-Line ""
Add-Line "[5] Ports used by the stack (80/443/8100/8300)"
foreach ($p in 80, 443, 8100, 8300) {
  $busy = $false
  try {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($c) { $busy = $true }
  } catch { }
  $who = ""
  if ($busy) {
    try {
      $own = (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
      $proc = Get-Process -Id $own -ErrorAction SilentlyContinue
      if ($proc) { $who = $proc.ProcessName + " (pid " + $own + ")" }
    } catch { }
  }
  Add-Item ("port " + $p) $(if ($busy) { "IN USE" } else { "free" }) $who
}

# ---------------------------------------------------------------- verdict
$blockers = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

if ($isServer -and $build -lt 20348) {
  [void]$blockers.Add("Windows Server 2019 and older only support WSL 1, which cannot run Docker. Need Server 2022 (build 20348) or newer.")
}
if (-not $isServer -and $build -lt 19041) {
  [void]$blockers.Add("Windows 10 build " + $build + " is too old. Need build 19041 or newer.")
}
if (-not $hyper) {
  [void]$blockers.Add("HypervisorPresent=False: nested virtualization is off or not exposed by the host. WSL 2 cannot start. On cloud VMs ask the provider to enable nested virtualization (many cheap plans cannot).")
}
if ($null -ne $vmFirmware -and -not $vmFirmware -and -not $hyper) {
  [void]$blockers.Add("CPU virtualization (VT-x/AMD-V) is disabled in BIOS/UEFI.")
}

Add-Line ""
Add-Line "============================================================"
if ($dockerWorks) {
  Add-Line " VERDICT: READY - docker daemon is already reachable."
  Add-Line " Next: copy the deploy package, then run forge-stack.ps1 -Action start"
  $code = 0
} elseif ($blockers.Count -eq 0) {
  Add-Line " VERDICT: INSTALLABLE - this host can run Linux containers via WSL 2."
  Add-Line " Next: run install-docker-wsl.ps1 as Administrator (one reboot may be needed)."
  $code = 2
} else {
  Add-Line " VERDICT: BLOCKED - do NOT try to install Docker on this host."
  foreach ($b in $blockers) { Add-Line ("   - " + $b) }
  Add-Line ""
  Add-Line " Use the bare-metal path instead (no containers):"
  Add-Line "   deploy\windows\install.ps1"
  Add-Line " It runs PostgreSQL + orchestrator + Label Studio as scheduled tasks."
  $code = 1
}
if ($warnings.Count -gt 0) {
  Add-Line ""
  Add-Line " Notes:"
  foreach ($w in $warnings) { Add-Line ("   - " + $w) }
}
Add-Line "============================================================"
Add-Line ""
Add-Line "Reminder: Docker Desktop itself is NOT supported on Windows Server."
Add-Line "This path installs Docker Engine inside WSL 2, which is a different thing."

if ($OutFile) {
  try {
    $lines -join "`r`n" | Out-File -FilePath $OutFile -Encoding utf8
    Write-Host ""
    Write-Host ("Report written to " + $OutFile)
  } catch {
    Write-Host ("[warn] could not write report: " + $_.Exception.Message)
  }
}

exit $code
