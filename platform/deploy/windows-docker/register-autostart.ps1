<#
.SYNOPSIS
  Register (or remove) the scheduled task that brings the FORGE stack up on boot.

.DESCRIPTION
  Why a scheduled task and not a Windows service:
    A Windows service hosting python.exe must call StartServiceCtrlDispatcher
    within 30 seconds or the SCM kills it with error 1053. Wrapping python needs
    an extra shell (NSSM / winsw / pywin32), and "download that exe" is often the
    one step that fails on an isolated server. A scheduled task needs nothing.

  Why it repeats every 5 minutes:
    the task is idempotent (`docker compose up -d`). If the host reboots while
    the WSL distribution is still initialising, the first attempt fails and the
    next one succeeds. It also restarts containers that died for whatever reason,
    with a worst-case recovery time of 5 minutes.

  Account matters:
    A WSL distribution is registered per user. A task running as SYSTEM cannot
    see a distribution registered by an administrator account, so `wsl -d ...`
    fails with "distribution not found". Therefore this script defaults to
    running the task as the account you are logged in with, and requires a
    password so it can also run while nobody is logged in.
    Use -OnLogon if you refuse to store the password: the stack then starts when
    that account logs on (typical for a server that stays logged in).

.EXAMPLE
  .\register-autostart.ps1 -StackDir D:\Forge\deploy
  .\register-autostart.ps1 -StackDir D:\Forge\deploy -OnLogon
  .\register-autostart.ps1 -Unregister
#>
param(
  [string]$TaskName = "ForgeStackDocker",
  [string]$Distro = "Ubuntu-22.04",
  [string]$StackDir = "D:\Forge\deploy",
  [string]$LogFile = "D:\Forge\data\logs\stack-autostart.log",
  [switch]$OnLogon,
  [System.Management.Automation.PSCredential]$Credential,
  [switch]$Unregister
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$m) { Write-Host ""; Write-Host ("== " + $m) -ForegroundColor Cyan }
function Write-Ok([string]$m) { Write-Host ("   [ok] " + $m) -ForegroundColor Green }
function Write-Warn2([string]$m) { Write-Host ("   [warn] " + $m) -ForegroundColor Yellow }
function Write-Err2([string]$m) { Write-Host ("   [fail] " + $m) -ForegroundColor Red }

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Err2 "must run as Administrator to register a scheduled task"
  exit 1
}

if ($Unregister) {
  Write-Step ("removing task " + $TaskName)
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Ok "removed"
  } else {
    Write-Ok "not present"
  }
  exit 0
}

$scriptPath = Join-Path $PSScriptRoot "forge-stack.ps1"
if (-not (Test-Path $scriptPath)) {
  Write-Err2 ("missing " + $scriptPath)
  exit 1
}
if (-not (Test-Path (Join-Path $StackDir "docker-compose.yml"))) {
  Write-Err2 ("no docker-compose.yml in " + $StackDir)
  exit 1
}

$logDir = Split-Path -Parent $LogFile
if ($logDir -and -not (Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

# ---------------------------------------------------------------- action
$inner = "& '" + $scriptPath + "' -Action start -Distro '" + $Distro + "' -StackDir '" + $StackDir + "' *>> '" + $LogFile + "'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command """ + $inner + """") `
  -WorkingDirectory $PSScriptRoot

# ---------------------------------------------------------------- triggers
Write-Step "building triggers"
$trigger = $null
if ($OnLogon) {
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  Write-Ok "trigger: at logon (no password stored)"
} else {
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $trigger.Delay = "PT45S"
  Write-Ok "trigger: at startup, 45 s delay (lets WSL and networking come up)"
}
# repeat every 5 minutes, forever: compose up -d is idempotent
try {
  $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
  $trigger.Repetition = $repeat.Repetition
} catch {
  Write-Warn2 ("could not attach repetition: " + $_.Exception.Message)
}

# ---------------------------------------------------------------- principal
Write-Step "choosing the account the task runs as"
if ($OnLogon) {
  $principal = New-ScheduledTaskPrincipal -UserId $id.Name -LogonType Interactive -RunLevel Highest
  Write-Ok ("runs as " + $id.Name + " (interactive)")
} else {
  if (-not $Credential) {
    Write-Host "   The autostart task must run as the account that owns the WSL distribution"
    Write-Host "   (a SYSTEM task cannot see a per-user WSL distro)."
    Write-Host "   Enter the password for " + $id.Name + " (stored by Windows Task Scheduler):"
    $Credential = Get-Credential -UserName $id.Name -Message "Account for the FORGE stack task"
    if (-not $Credential) { Write-Err2 "cancelled"; exit 1 }
  }
  $principal = New-ScheduledTaskPrincipal -UserId $Credential.UserName -LogonType Password -RunLevel Highest
  Write-Ok ("runs as " + $Credential.UserName + " (whether logged on or not)")
}

# ---------------------------------------------------------------- settings
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -MultipleInstances IgnoreNew
$settings.ExecutionTimeLimit = "PT0S"
$settings.RestartCount = 3
$settings.RestartInterval = "PT2M"

# ---------------------------------------------------------------- register
Write-Step ("registering " + $TaskName)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Ok "replaced existing task"
}
if ($OnLogon) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
} else {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -User $Credential.UserName `
    -Password $Credential.GetNetworkCredential().Password | Out-Null
}
Write-Ok "registered"

# ---------------------------------------------------------------- verify
Write-Step "starting it once to verify"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 20
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ("   last run  : " + $info.LastRunTime)
Write-Host ("   last result: " + $info.LastTaskResult + "  (0 = success, 267009 = still running)")
if ($info.LastTaskResult -ne 0 -and $info.LastTaskResult -ne 267009) {
  Write-Warn2 "non-zero result, check the log:"
  Write-Host ("   " + $LogFile)
}
if (Test-Path $LogFile) {
  Write-Step "tail of " + $LogFile
  Get-Content $LogFile -Tail 30
}

Write-Host ""
Write-Host "Manage:"
Write-Host ("   Get-ScheduledTask -TaskName " + $TaskName + " | Get-ScheduledTaskInfo")
Write-Host ("   Start-ScheduledTask -TaskName " + $TaskName)
Write-Host ("   .\register-autostart.ps1 -Unregister")
