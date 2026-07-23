# One-command release: builds Meridian.exe and publishes it to your GitHub Releases, so installed copies
# auto-update. Needs the GitHub CLI (gh) authenticated (https://cli.github.com, then `gh auth login`) and
# this folder to be a git repo with a GitHub remote.
#     powershell -ExecutionPolicy Bypass -File release.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$ver = (Select-String -Path .\app.py -Pattern 'APP_VERSION\s*=\s*"([\d.]+)"').Matches[0].Groups[1].Value
if (-not $ver) { throw "APP_VERSION not found in app.py" }
$tag = "v$ver"
Write-Host "Releasing Meridian $tag"

powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
$exe = Join-Path $PSScriptRoot 'dist\Meridian.exe'
if (-not (Test-Path $exe)) { throw "build failed - dist\Meridian.exe missing" }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "GitHub CLI (gh) not found. Either:"
    Write-Host "  - install it from https://cli.github.com, run 'gh auth login', then re-run this; or"
    Write-Host "  - on GitHub, create a Release tagged $tag and upload dist\Meridian.exe as an asset."
    exit 0
}
gh release create $tag $exe --title "Meridian $ver" --notes "Meridian $ver"
Write-Host "Published $tag with Meridian.exe. Installed copies will offer the update on next launch."
