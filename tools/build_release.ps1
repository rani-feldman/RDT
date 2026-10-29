<#
.SYNOPSIS
Build an RDT release on Windows: the installer, the portable zip and their checksums (INS-6, INS-8).

.DESCRIPTION
The release workflow runs this on GitHub; it also runs on your own PC (needs Python 3.11+ and
Inno Setup 6 from https://jrsoftware.org/isdl.php). Output goes to release\:
    RDT-Setup-<version>.exe, RDT-<version>-portable.zip, SHA256SUMS.txt
The version is the one in rdt\_version.py (set it with: python tools\release.py prepare 1.2.0).

Signing (INS-10) is off until a certificate is set up: set RDT_SIGN_CERT to a .pfx file and
RDT_SIGN_PASSWORD to its password, and signtool.exe (Windows SDK) signs every exe.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\build_release.ps1
powershell -ExecutionPolicy Bypass -File tools\build_release.ps1 -SkipTests
#>
param(
    [switch]$SkipTests,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Run([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe $($arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Find-Iscc {
    $found = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($dir in @("${env:ProgramFiles(x86)}\Inno Setup 6", "$env:ProgramFiles\Inno Setup 6",
                       "$env:LOCALAPPDATA\Programs\Inno Setup 6")) {
        if (Test-Path "$dir\ISCC.exe") { return "$dir\ISCC.exe" }
    }
    throw "Inno Setup 6 is not installed: get it from https://jrsoftware.org/isdl.php"
}

function Sign-Files([string[]]$files) {
    if (-not $env:RDT_SIGN_CERT) { Write-Host "Not signing: RDT_SIGN_CERT is not set"; return }
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
    if (-not $signtool) { throw "RDT_SIGN_CERT is set but signtool.exe (Windows SDK) is not on PATH" }
    foreach ($f in $files) {
        Run $signtool @("sign", "/f", $env:RDT_SIGN_CERT, "/p", $env:RDT_SIGN_PASSWORD, "/fd", "SHA256",
                        "/tr", "http://timestamp.digicert.com", "/td", "SHA256", $f)
    }
}

$Version = (& $Python tools\release.py version).Trim()
Write-Host "Building RDT $Version"

Run $Python @("-m", "pip", "install", "-e", ".[dev]", "pyinstaller>=6.10")
if (-not $SkipTests) {
    $env:QT_QPA_PLATFORM = "offscreen"
    Run $Python @("-m", "pytest", "-q")
    Remove-Item Env:QT_QPA_PLATFORM
}

foreach ($dir in @("build", "dist", "release")) { if (Test-Path $dir) { Remove-Item -Recurse -Force $dir } }
Run $Python @("packaging\version_info.py", $Version, "build\version_info.txt")
Run $Python @("-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
              "--distpath", "dist", "--workpath", "build\pyinstaller", "packaging\rdt.spec")

# the example workspaces and the simulator's files, beside the exes (both the installer and the zip);
# the bench.* files are the default workspace the GUI starts with (WSP-9)
$App = Join-Path $Root "dist\RDT"
New-Item -ItemType Directory -Force "$App\examples", "$App\sim" | Out-Null
Copy-Item examples\sim_bench.rdtw, examples\sim_bench.watch.toml, examples\sim_target.toml, examples\bench.* "$App\examples"
Copy-Item -Recurse sim\examples "$App\sim\examples"
Sign-Files (Get-ChildItem "$App\*.exe").FullName

New-Item -ItemType Directory -Force release | Out-Null
$Release = Join-Path $Root "release"
Run (Find-Iscc) @("/Q", "/DAppVersion=$Version", "/DSourceDir=$App", "/DOutputDir=$Release", "packaging\rdt.iss")
$Setup = "$Release\RDT-Setup-$Version.exe"
Sign-Files @($Setup)

$Zip = "$Release\RDT-$Version-portable.zip"
Compress-Archive -Path $App -DestinationPath $Zip

$sums = Get-ChildItem $Setup, $Zip | ForEach-Object {
    "$((Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower())  $($_.Name)"
}
Set-Content -Path "$Release\SHA256SUMS.txt" -Value $sums -Encoding ascii
Write-Host ""
Write-Host "Done:"
Get-ChildItem $Release | ForEach-Object { Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
