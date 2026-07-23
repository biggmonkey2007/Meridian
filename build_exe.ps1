# Builds Meridian into a standalone, windowed Windows program: dist\Meridian.exe
# (no console window, custom globe icon, no Python needed on the target machine).
# Run it after changing app.py or meridian-relief.html to refresh the .exe:
#     powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "1/3  Regenerating icon..."
python make_icon.py

Write-Host "2/3  Ensuring PyInstaller is installed..."
python -m pip install --quiet --disable-pip-version-check pyinstaller

Write-Host "3/3  Building Meridian.exe (this takes a few minutes)..."
python -m PyInstaller --noconsole --onefile --name Meridian --icon meridian.ico `
    --add-data "meridian-relief.html;." --add-data "country_grades.json;." `
    --collect-all webview --noconfirm app.py

$exe = Join-Path $PSScriptRoot 'dist\Meridian.exe'
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Done -> $exe"
    Write-Host "Double-click it to run Meridian. Data (cache, channels.txt) lives in %LOCALAPPDATA%\Meridian."
} else {
    Write-Host "Build finished but dist\Meridian.exe was not found - check the PyInstaller output above."
}
