# Removes Meridian (program files + shortcuts + registry entry). Your saved data
# (%LOCALAPPDATA%\Meridian — cache, starred countries, read history) is kept.
$ErrorActionPreference = 'SilentlyContinue'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\Meridian'

Get-Process Meridian -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

Remove-Item (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Meridian.lnk') -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Meridian.lnk') -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Meridian' -Recurse -Force

# The script lives inside $installDir, so it can't delete its own folder while running — schedule the
# removal to happen a couple of seconds after this process exits.
Start-Process cmd -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$installDir`"" -WindowStyle Hidden
Write-Host "Meridian uninstalled. (Your data in %LOCALAPPDATA%\Meridian was kept.)"
