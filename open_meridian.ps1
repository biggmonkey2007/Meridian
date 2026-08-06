# Auto-opens the Meridian desktop app after Claude Code finishes an update.
# Wired up as a Stop hook in .claude/settings.json. Safe to run by hand too.
#
# It only (re)launches when meridian-relief.html or app.py changed since the last
# open, and on relaunch it closes EVERY Meridian window still open (by window title,
# so untracked/manually-opened ones are caught too) so they never pile up.
$ErrorActionPreference = 'SilentlyContinue'

$dir     = 'C:\Users\minta\OneDrive\Desktop\Meridian'
$state   = Join-Path $dir 'cache'
if (-not (Test-Path $state)) { New-Item -ItemType Directory -Path $state | Out-Null }
$marker  = Join-Path $state '.meridian_opened'   # its mtime = when we last opened
$pidFile = Join-Path $state '.meridian.pid'      # PID of the window we opened

# newest mtime across the app's source files
$sources = @('meridian-relief.html','app.py') | ForEach-Object { Join-Path $dir $_ } | Where-Object { Test-Path $_ }
if (-not $sources) { exit 0 }
$newest = ($sources | ForEach-Object { (Get-Item $_).LastWriteTime } | Measure-Object -Maximum).Maximum

# nothing changed since the last open -> do nothing (so plain chat turns don't relaunch)
if ((Test-Path $marker) -and $newest -le (Get-Item $marker).LastWriteTime) { exit 0 }

# close EVERY Meridian window still open (its window title is "Meridian"), not just the one we
# tracked — so windows can never pile up across relaunches. Fall back to the tracked PID in case a
# window's title wasn't ready yet.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*app.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
# ALSO close the installed Meridian.exe — otherwise the dev copy we relaunch and the exe both bind the
# app's fixed port (49731), one goes black, and windows pile up. One Meridian at a time, always.
Get-Process 'Meridian' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
if (Test-Path $pidFile) {
  $old = Get-Content $pidFile
  if ($old) { Stop-Process -Id ([int]$old) -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 300

# resolve pythonw (fall back to python), then launch from the app's own folder
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
  $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if ($py) { $c = Join-Path (Split-Path $py) 'pythonw.exe'; if (Test-Path $c) { $pyw = $c } else { $pyw = $py } }
}
if (-not $pyw) { exit 0 }

$proc = Start-Process -FilePath $pyw -ArgumentList 'app.py' -WorkingDirectory $dir -PassThru -ErrorAction SilentlyContinue
if ($proc) { Set-Content -Path $pidFile -Value $proc.Id }

# stamp the marker to "now" so we won't reopen until the next edit
if (Test-Path $marker) { (Get-Item $marker).LastWriteTime = Get-Date } else { New-Item -ItemType File -Path $marker | Out-Null }
exit 0
