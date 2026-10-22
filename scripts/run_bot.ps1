# Runs the bot. Restarts it 5 seconds after a crash (nonzero exit).
# A clean exit (/stop or Ctrl+C, exit code 0) ends the loop.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $root ".venv\Scripts\python.exe" }

while ($true) {
    & $py bot.py
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
