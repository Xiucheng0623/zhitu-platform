<#
FORGE platform - Windows server installer
=============================================================================
Why Task Scheduler instead of sc.exe / NSSM:

  * sc.exe create with a plain python.exe does NOT work. A Windows service
    process must call StartServiceCtrlDispatcher within 30s or the SCM kills
    it with error 1053. There is no way around this without a service
    wrapper (pywin32 / NSSM / winsw), and this server has none of them.
  * pywin32 and NSSM both need a download. On a locked-down intranet that
    download is often the one thing that fails, and then the whole install
    script is useless.
  * A scheduled task with trigger AtStartup, principal SYSTEM,
    RunLevel Highest is a real boot-time service: it starts before any user
    logs in, survives reboots, and needs zero third-party binaries.

Trade-off you should know about: Task Scheduler has no automatic restart on
crash. This script therefore adds a repetition trigger (every 1 minute,
indefinitely) so a dead process is relaunched within a minute. It is not as
clean as NSSM's AppExit handling, but it is honest and dependency-free.

Usage (run in an elevated PowerShell):
  .\install.ps1 -AppRoot D:\Forge\app -DataRoot D:\Forge\data `
                -ApiPort 8300 -LSPort 8080

  .\install.ps1 ... -SkipVenv        # reuse existing venv
  .\install.ps1 ... -SkipLabelStudio # platform only, no LS task
  .\install.ps1 ... -Uninstall

Everything this script writes lives under -DataRoot:
  config\forge.env          secrets + settings (ACL: Administrators + SYSTEM)
  logs\                     stdout/stderr of each task
  run-orchestrator.cmd      launcher (env vars are baked in, no parser bugs)
  run-labelstudio.cmd
#>

[CmdletBinding()]
param(
  [string]$AppRoot = "D:\Forge\app",
  [string]$DataRoot = "D:\Forge\data",
  [int]$ApiPort = 8300,
  [int]$LSPort = 8080,
  [string]$Domain = "",
  [string]$AdminPhone = "",
  [string]$PyExe = "",
  [switch]$SkipVenv,
  [switch]$SkipLabelStudio,
  [switch]$SkipMigrations,
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskApi = "ForgeOrchestrator"
$TaskLS = "ForgeLabelStudio"

function Write-Step([string]$msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg) { Write-Host "   [ok] $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "   [warn] $msg" -ForegroundColor Yellow }
function Write-Err2([string]$msg) { Write-Host "   [fail] $msg" -ForegroundColor Red }

function Assert-Admin {
  $p = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must run in an elevated PowerShell (Run as Administrator)."
  }
}

function New-Secret([int]$bytes) {
  $buf = New-Object byte[] $bytes
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
  return ($buf | ForEach-Object { $_.ToString("x2") }) -join ""
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if ($Uninstall) {
  Assert-Admin
  Write-Step "Removing scheduled tasks"
  foreach ($t in @($TaskApi, $TaskLS)) {
    $found = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($found) {
      Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $t -Confirm:$false
      Write-Ok "removed $t"
    } else {
      Write-Warn2 "$t not present"
    }
  }
  Write-Host ""
  Write-Host "Data under $DataRoot was NOT deleted (database, uploads, logs)."
  Write-Host "Remove it manually only after you have taken a backup."
  exit 0
}

Assert-Admin

# ---------------------------------------------------------------------------
# 0. Paths
# ---------------------------------------------------------------------------
Write-Step "Layout"
$OrchDir = Join-Path $AppRoot "services\orchestrator"
$WebDist = Join-Path $AppRoot "web\dist"
$VenvDir = Join-Path $AppRoot "venv"
$CfgDir = Join-Path $DataRoot "config"
$LogDir = Join-Path $DataRoot "logs"

if (-not (Test-Path $OrchDir)) { throw "orchestrator sources not found: $OrchDir" }
foreach ($d in @($CfgDir, $LogDir)) {
  if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Write-Ok "app    = $AppRoot"
Write-Ok "data   = $DataRoot"

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
Write-Step "Python interpreter"
if (-not $PyExe) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "python not found in PATH. Install Python 3.11+ or pass -PyExe." }
  $PyExe = $cmd.Source
}
$ver = & $PyExe -c "import sys;print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10") { throw "Python $ver is too old, need 3.10+." }
Write-Ok "$PyExe (Python $ver)"

# ---------------------------------------------------------------------------
# 2. venv + deps
# ---------------------------------------------------------------------------
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if ($SkipVenv) {
  if (-not (Test-Path $VenvPy)) { throw "-SkipVenv given but $VenvPy does not exist." }
  Write-Ok "reusing $VenvDir"
} else {
  Write-Step "Creating venv (this takes a minute)"
  if (-not (Test-Path $VenvPy)) {
    & $PyExe -m venv $VenvDir
  }
  $req = Join-Path $OrchDir "requirements.txt"
  # Intranet often blocks pypi.org. Aliyun mirror is reachable in CN networks.
  & $VenvPy -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --quiet
  & $VenvPy -m pip install -r $req -i https://mirrors.aliyun.com/pypi/simple/
  if ($LASTEXITCODE -ne 0) { throw "pip install failed. Check network / mirror." }
  Write-Ok "dependencies installed"
}

# ---------------------------------------------------------------------------
# 3. Config (idempotent: keep existing secrets, only fill what is missing)
# ---------------------------------------------------------------------------
Write-Step "Configuration"
$EnvFile = Join-Path $CfgDir "forge.env"
$cfg = @{}
if (Test-Path $EnvFile) {
  Get-Content $EnvFile | ForEach-Object {
    if ($_ -match "^\s*#" -or $_ -notmatch "=") { return }
    $k, $v = $_.Split("=", 2)
    $cfg[$k.Trim()] = $v.Trim()
  }
  Write-Ok "existing config loaded ($($cfg.Count) keys)"
}
if (-not $cfg["FORGE_JWT_SECRET"] -or $cfg["FORGE_JWT_SECRET"].Length -lt 32) {
  $cfg["FORGE_JWT_SECRET"] = New-Secret 48
  Write-Ok "generated FORGE_JWT_SECRET"
}
# Shared secret for the platform -> Label Studio SSO ticket.
# Both processes MUST see the same value; changing it only invalidates
# in-flight 90-second tickets, not existing LS sessions.
if (-not $cfg["FORGE_LS_SSO_SECRET"] -or $cfg["FORGE_LS_SSO_SECRET"].Length -lt 32) {
  $cfg["FORGE_LS_SSO_SECRET"] = New-Secret 48
  Write-Ok "generated FORGE_LS_SSO_SECRET"
}
if (-not $cfg["FORGE_DB_DSN"]) {
  # Deliberately NOT defaulting to a baked-in password. The installer refuses
  # to guess; the operator must supply the app DB password on first run.
  $secure = Read-Host "Password for PostgreSQL role forge_app" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  $cfg["FORGE_DB_DSN"] = "postgresql://forge_app:$plain@127.0.0.1:5432/forge"
  Write-Warn2 "DSN written to $EnvFile - make sure that file is not world-readable"
}
$cfg["FORGE_AUTH_MODE"] = "native"
$cfg["FORGE_HOST"] = "127.0.0.1"
$cfg["FORGE_PORT"] = "$ApiPort"
$cfg["FORGE_JWT_ISSUER"] = "forge-orchestrator"
$cfg["FORGE_JWT_AUDIENCE"] = "forge-platform"
$cfg["FORGE_JWT_TTL_MIN"] = "480"
$cfg["FORGE_LOGIN_MAX_FAILURES"] = "5"
$cfg["FORGE_LOGIN_LOCK_MIN"] = "15"
# NIST SP 800-63B floor is 8. Raise it if your security policy demands,
# but length alone is the weakest lever - lockout matters more.
$cfg["FORGE_PASSWORD_MIN_LEN"] = "8"
if ($Domain) {
  $cfg["FORGE_CORS_ORIGINS"] = "https://$Domain,https://ls.$Domain"
} elseif (-not $cfg["FORGE_CORS_ORIGINS"]) {
  $cfg["FORGE_CORS_ORIGINS"] = "http://localhost:9121"
}
$cfg["FORGE_LS_BASE_URL"] = "http://127.0.0.1:$LSPort"
# Browser-reachable LS origin. Server-side calls use FORGE_LS_BASE_URL,
# which is often an internal container name the browser cannot resolve.
# NOTE: platform and LS must share the same registrable domain, otherwise
# Django's SameSite=Lax session cookie will not be sent in the iframe.
if ($Domain) {
  $cfg["FORGE_LS_PUBLIC_BASE_URL"] = "https://ls.$Domain"
} else {
  $cfg["FORGE_LS_PUBLIC_BASE_URL"] = "http://127.0.0.1:$LSPort"
}

$lines = @(
  "# FORGE runtime configuration - generated by install.ps1",
  "# Contains secrets. Restrict this file to Administrators + SYSTEM.",
  "# Never commit it."
)
foreach ($k in ($cfg.Keys | Sort-Object)) { $lines += "$k=$($cfg[$k])" }
[IO.File]::WriteAllLines($EnvFile, $lines, (New-Object Text.UTF8Encoding($false)))
Write-Ok $EnvFile

# ACL: only Administrators and SYSTEM. The DB password sits in here.
& icacls $EnvFile /inheritance:r /grant:r "BUILTIN\Administrators:(R,W)" "NT AUTHORITY\SYSTEM:(R)" | Out-Null
Write-Ok "ACL restricted on $EnvFile"

# ---------------------------------------------------------------------------
# 4. Launchers
# ---------------------------------------------------------------------------
# The launchers bake env vars in as literal `set` lines. Parsing a .env file in
# cmd.exe is a footgun (values with '=' or '&' silently truncate), and a
# scheduled task cannot carry an environment block.
Write-Step "Launchers"

$orch = @("@echo off", "setlocal")
foreach ($k in ($cfg.Keys | Sort-Object)) { $orch += "set `"$k=$($cfg[$k])`"" }
$orch += "cd /d `"$OrchDir`""
$orch += "`"$VenvPy`" -m app.serve 1>>`"$LogDir\orchestrator.log`" 2>>`"$LogDir\orchestrator.err.log`""
$orchPath = Join-Path $DataRoot "run-orchestrator.cmd"
[IO.File]::WriteAllLines($orchPath, $orch, (New-Object Text.UTF8Encoding($false)))
Write-Ok $orchPath

# ---------------------------------------------------------------------------
# 5. Scheduled tasks
# ---------------------------------------------------------------------------
function Register-ForgeTask {
  param([string]$Name, [string]$CmdPath, [string]$WorkDir)

  $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if ($existing) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
  }
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$CmdPath`"" -WorkingDirectory $WorkDir
  $boot = New-ScheduledTaskTrigger -AtStartup
  # Repetition on the boot trigger: restart within 60s if the process dies.
  $boot.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
      -RepetitionInterval (New-TimeSpan -Minutes 1) `
      -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
      -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $boot `
      -Principal $principal -Settings $set `
      -Description "FORGE platform ($Name)" | Out-Null
  Write-Ok "task $Name registered"
}

Write-Step "Registering tasks"
Register-ForgeTask -Name $TaskApi -CmdPath $orchPath -WorkDir $OrchDir
Start-ScheduledTask -TaskName $TaskApi
Start-Sleep -Seconds 4
$listen = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue
if ($listen) {
  Write-Ok "orchestrator listening on 127.0.0.1:$ApiPort"
} else {
  Write-Warn2 "orchestrator is not listening yet - check $LogDir\orchestrator.err.log"
}

# ---------------------------------------------------------------------------
# 6. Label Studio
# ---------------------------------------------------------------------------
if (-not $SkipLabelStudio) {
  Write-Step "Label Studio launcher"
  $lsExe = Join-Path $AppRoot "lsvenv\Scripts\label-studio.exe"
  if (-not (Test-Path $lsExe)) {
    Write-Warn2 "not found: $lsExe"
    Write-Warn2 "install it first:  python -m venv $AppRoot\lsvenv"
    Write-Warn2 "                   $AppRoot\lsvenv\Scripts\pip install label-studio==1.23.2"
    Write-Warn2 "then re-run this script without -SkipLabelStudio"
  } else {
    $lsData = Join-Path $DataRoot "labelstudio"
    if (-not (Test-Path $lsData)) { New-Item -ItemType Directory -Path $lsData -Force | Out-Null }
    $lsLines = @(
      "@echo off",
      "setlocal",
      "set `"LABEL_STUDIO_BASE_DATA_DIR=$lsData`"",
      "rem LS reads DISABLE_SIGNUP_WITHOUT_LINK with NO LABEL_STUDIO_ prefix.",
      "set `"DISABLE_SIGNUP_WITHOUT_LINK=true`"",
      "rem Must match FORGE_LS_SSO_SECRET in run-orchestrator.cmd.",
      "set `"FORGE_LS_SSO_SECRET=$($cfg['FORGE_LS_SSO_SECRET'])`"",
      "set `"USE_DEFAULT_BANNED_SUBNETS=false`""
    )
    if ($cfg["LS_SECRET_KEY"]) {
      $lsLines += "set `"DJANGO_SECRET_KEY=$($cfg['LS_SECRET_KEY'])`""
    }
    if ($cfg["LS_POSTGRE_NAME"]) {
      $lsLines += "set `"DJANGO_DB=default`""
      $lsLines += "set `"POSTGRE_NAME=$($cfg['LS_POSTGRE_NAME'])`""
      $lsLines += "set `"POSTGRE_USER=$($cfg['LS_POSTGRE_USER'])`""
      $lsLines += "set `"POSTGRE_PASSWORD=$($cfg['LS_POSTGRE_PASSWORD'])`""
      $lsLines += "set `"POSTGRE_HOST=127.0.0.1`""
      $lsLines += "set `"POSTGRE_PORT=5432`""
    } else {
      $lsLines += "rem WARNING: still on SQLite. Multi-user write contention will bite."
    }
    $lsLines += "`"$lsExe`" start --port $LSPort --data-dir `"$lsData`" --no-browser 1>>`"$LogDir\labelstudio.log`" 2>>`"$LogDir\labelstudio.err.log`""
    $lsCmd = Join-Path $DataRoot "run-labelstudio.cmd"
    [IO.File]::WriteAllLines($lsCmd, $lsLines, (New-Object Text.UTF8Encoding($false)))
    Register-ForgeTask -Name $TaskLS -CmdPath $lsCmd -WorkDir (Join-Path $AppRoot "lsvenv\Scripts")
    # SSO middleware lives in the brand patch, not in the pip package.
    # Without it, /forge/sso/ does not exist and the iframe returns 404.
    Write-Warn2 "before opening the platform, run: python platform\labelstudio\brand\apply_brand.py"
    Start-ScheduledTask -TaskName $TaskLS
    Write-Ok "Label Studio task started"
  }
}

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------
Write-Step "Done"
Write-Host @"
   API        http://127.0.0.1:$ApiPort/healthz
   Web        build output: $WebDist
   Logs       $LogDir
   Config     $EnvFile

Next steps:
   1. Create the first administrator (prints a one-time password):
        cd "$OrchDir"
        "$VenvPy" -m tools.bootstrap_admin --phone <11-digit-mobile> --name "<name>"

   2. Serve the frontend + TLS. Without a reverse proxy the browser talks
      plain HTTP to $ApiPort and passwords cross the LAN in clear text.
      See deploy\windows\README.md section 'HTTPS'.

   3. Log in, change the initial password, then create invite codes under
      Settings > Users to onboard the team.
"@
