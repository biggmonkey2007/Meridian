# Run this ONCE from inside the Meridian folder AFTER you've moved it out of OneDrive.
# It detects wherever the folder now lives and rewrites the two files that hard-code the old path
# (the auto-open launcher and the Claude Code hook). Then you can delete this file.
$new = $PSScriptRoot
$old = 'C:\Users\minta\OneDrive\Desktop\Meridian'
foreach ($rel in @('open_meridian.ps1', '.claude\settings.json')) {
    $p = Join-Path $new $rel
    if (Test-Path $p) {
        $c = Get-Content $p -Raw
        # handle both plain paths (the .ps1) and JSON-escaped backslashes (settings.json)
        $c = $c.Replace($old, $new).Replace($old.Replace('\', '\\'), $new.Replace('\', '\\'))
        Set-Content -Path $p -Value $c -NoNewline -Encoding utf8
        Write-Host "fixed: $rel"
    }
}
Write-Host "Done. Meridian now points at: $new"
