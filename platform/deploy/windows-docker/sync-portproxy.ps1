<#
.SYNOPSIS
  Publish WSL 2 container ports on the Windows host's LAN interface.

.DESCRIPTION
  The trap this solves:
    WSL 2 runs in a lightweight VM with its own IP. Docker publishes ports inside
    that VM. `localhost:80` works from Windows because WSL forwards it, but
    `<server-ip>:80` from another machine does NOT - nothing is listening on the
    Windows interface. The fix is a netsh portproxy rule pointing at the VM IP.
    The VM IP changes on every reboot, so the rule must be refreshed on every
    start. That is why forge-stack.ps1 calls this script after `compose up`.

  Requires Administrator (netsh portproxy + firewall rules).

  Safe to re-run: rules for the given ports are deleted then re-added.

.EXAMPLE
  .\sync-portproxy.ps1
  .\sync-portproxy.ps1 -Ports 80,443
#>
param(
  [string]$Distro = "Ubuntu-22.04",
  [int[]]$Ports = @(80, 443),
  [string]$ListenAddress = "0.0.0.0",
  [switch]$NoFirewall
)

$ErrorActionPreference = "Continue"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "   [warn] not administrator, portproxy not updated (LAN access will not work)"
  exit 2
}

# free-form IP discovery: hostname -I may return several addresses (docker0 etc.)
$raw = & wsl.exe -d $Distro -u root -- hostname -I 2>&1 | Out-String
$wslIp = $null
foreach ($cand in ($raw -split "\s+")) {
  $t = $cand.Trim()
  if ($t -match "^\d{1,3}(\.\d{1,3}){3}$" -and -not $t.StartsWith("172.17.") -and -not $t.StartsWith("127.")) {
    $wslIp = $t
    break
  }
}
if (-not $wslIp) {
  Write-Host ("   [warn] could not determine WSL ip: " + $raw.Trim())
  exit 1
}
Write-Host ("   WSL ip: " + $wslIp)

foreach ($p in $Ports) {
  & netsh.exe interface portproxy delete v4tov4 listenport=$p listenaddress=$ListenAddress 2>&1 | Out-Null
  & netsh.exe interface portproxy add v4tov4 listenport=$p listenaddress=$ListenAddress connectport=$p connectaddress=$wslIp 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Host ("   [ok] " + $ListenAddress + ":" + $p + " -> " + $wslIp + ":" + $p)
  } else {
    Write-Host ("   [fail] portproxy for port " + $p)
  }
  if (-not $NoFirewall) {
    $ruleName = "FORGE port " + $p
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existing) {
      New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $p -Profile Any -ErrorAction SilentlyContinue | Out-Null
    }
  }
}

Write-Host "   current rules:"
& netsh.exe interface portproxy show v4tov4 | Out-String | Write-Host
exit 0
