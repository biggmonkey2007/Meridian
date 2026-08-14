# Per-user installer for Meridian — no admin needed. Installs to %LOCALAPPDATA%\Programs\Meridian,
# adds Start-Menu + Desktop shortcuts, and registers an uninstaller so it shows in Settings > Apps.
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$exeSrc = Join-Path $src 'dist\Meridian.exe'
if (-not (Test-Path $exeSrc)) { Write-Host "dist\Meridian.exe not found - run build_exe.ps1 first."; exit 1 }

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\Meridian'
# stop a running copy so we can overwrite it
Get-Process Meridian -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item $exeSrc (Join-Path $installDir 'Meridian.exe') -Force
if (Test-Path (Join-Path $src 'meridian.ico')) { Copy-Item (Join-Path $src 'meridian.ico') (Join-Path $installDir 'meridian.ico') -Force }
Copy-Item (Join-Path $src 'uninstall.ps1') (Join-Path $installDir 'uninstall.ps1') -Force

$exe = Join-Path $installDir 'Meridian.exe'
$ico = Join-Path $installDir 'meridian.ico'

function New-Shortcut([string]$path) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $installDir
    $s.IconLocation = "$ico,0"
    $s.Description = 'Meridian - world news, mapped'
    $s.Save()
}
New-Shortcut (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Meridian.lnk')
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Meridian.lnk')

# read the single source of truth for the version out of app.py
$ver = '1.0.0'
$m = Select-String -Path (Join-Path $src 'app.py') -Pattern 'APP_VERSION\s*=\s*"([\d.]+)"' -ErrorAction SilentlyContinue
if ($m) { $ver = $m.Matches[0].Groups[1].Value }

$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Meridian'
New-Item -Path $key -Force | Out-Null
Set-ItemProperty $key DisplayName    'Meridian'
Set-ItemProperty $key DisplayVersion $ver
Set-ItemProperty $key Publisher      'Meridian'
Set-ItemProperty $key DisplayIcon    $ico
Set-ItemProperty $key InstallLocation $installDir
Set-ItemProperty $key UninstallString "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $installDir 'uninstall.ps1')`""
Set-ItemProperty $key EstimatedSize  ([int][math]::Round((Get-Item $exe).Length / 1KB)) -Type DWord
Set-ItemProperty $key NoModify 1 -Type DWord
Set-ItemProperty $key NoRepair 1 -Type DWord

# Auto-launch the freshly-installed build so an update can be tested right away (no manual re-open).
Start-Process -FilePath $exe -WorkingDirectory $installDir

Write-Host ""
Write-Host "Meridian installed to $installDir and launched."
Write-Host "Also available from the Start Menu or the Desktop shortcut."
Write-Host "Uninstall any time from Settings > Apps > Meridian."
