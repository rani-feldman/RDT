<#
.SYNOPSIS
Install, upgrade and uninstall an RDT setup silently and check each step (INS-8). The release
workflow runs it on every build; it changes the current user's RDT installation, so run it on a
build machine, not on a PC whose RDT you use.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File packaging\smoke_test.ps1 -Setup release\RDT-Setup-1.2.0.exe -Version 1.2.0
#>
param(
    [Parameter(Mandatory)][string]$Setup,
    [Parameter(Mandatory)][string]$Version
)

$ErrorActionPreference = "Stop"
$App = "$env:LOCALAPPDATA\Programs\RDT"
$Settings = "$env:LOCALAPPDATA\rdt"
$Marker = "$Settings\smoke-test-marker.txt"
$Log = Join-Path (Split-Path -Parent (Resolve-Path $Setup)) "smoke-test"
New-Item -ItemType Directory -Force $Log | Out-Null

function Check([bool]$ok, [string]$what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "ok: $what"
}

function Install([string]$name) {
    $p = Start-Process -FilePath $Setup -PassThru -Wait -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=addtopath", "/LOG=$Log\$name.log")
    Check ($p.ExitCode -eq 0) "$name exits with 0 (got $($p.ExitCode))"
}

function UserPath { [Environment]::GetEnvironmentVariable("Path", "User") }

function Wait-Gone([string]$path, [int]$seconds) {
    for ($i = 0; $i -lt $seconds -and (Test-Path $path); $i++) { Start-Sleep 1 }
    -not (Test-Path $path)
}

# settings a user already has, which every step must keep
New-Item -ItemType Directory -Force $Settings | Out-Null
Set-Content $Marker "keep me"

Install "install"
foreach ($exe in "rdt-gui.exe", "rdt.exe", "rdt-sim.exe", "unins000.exe") {
    Check (Test-Path "$App\$exe") "$exe installed"
}
Check (Test-Path "$App\_internal\docs\CHANGELOG.md") "release notes installed"
Check (Test-Path "$App\examples\sim_bench.rdtw") "example workspace installed"
$v = (& "$App\rdt.exe" --version) -join ""
Check ($v -eq "rdt $Version") "rdt --version says rdt $Version (got '$v')"
Check ((UserPath) -split ";" -contains $App) "install folder on the user's PATH"
Check ((Get-ItemProperty "HKCU:\Software\Classes\.rdtw")."(default)" -eq "RDT.Workspace") ".rdtw opens in RDT"

# the CLI reads a variable from the installed simulator
$sim = Start-Process -FilePath "$App\rdt-sim.exe" -PassThru `
    -RedirectStandardOutput "$Log\sim.out" -RedirectStandardError "$Log\sim.err"
Start-Sleep 4
$read = & "$App\rdt.exe" -w "$App\examples\sim_bench.rdtw" read main:motor.speed 2>&1
$code = $LASTEXITCODE
Stop-Process -Id $sim.Id -Force
Check ($code -eq 0) "rdt reads main:motor.speed from the simulator ($read)"

# the GUI starts and keeps running (offscreen: the build machine may have no desktop)
$env:QT_QPA_PLATFORM = "offscreen"
$gui = Start-Process -FilePath "$App\rdt-gui.exe" -ArgumentList "`"$App\examples\sim_bench.rdtw`"" -PassThru
Start-Sleep 10
Check (-not $gui.HasExited) "rdt-gui.exe still running after 10 s"
Stop-Process -Id $gui.Id -Force
Remove-Item Env:QT_QPA_PLATFORM
Start-Sleep 2

# an upgrade (here the same setup again) removes the old install first and keeps the settings
Install "upgrade"
Check (Test-Path "$App\rdt-gui.exe") "rdt-gui.exe there after the upgrade"
Check (Test-Path $Marker) "user settings kept by the upgrade"

# a silent uninstall removes the program and the PATH entry, and keeps the settings
$p = Start-Process -FilePath "$App\unins000.exe" -PassThru -Wait -ArgumentList @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$Log\uninstall.log")
Check (Wait-Gone "$App\rdt-gui.exe" 60) "program removed by the uninstall"
Check (-not ((UserPath) -split ";" -contains $App)) "install folder taken off the PATH"
Check (-not (Test-Path "HKCU:\Software\Classes\RDT.Workspace")) ".rdtw association removed"
Check (Test-Path $Marker) "user settings kept by the uninstall"
Remove-Item $Marker
Write-Host "Smoke test passed"
