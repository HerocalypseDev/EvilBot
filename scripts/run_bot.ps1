# Runs the bot. Restarts it 5 seconds after a crash (nonzero exit).
# A clean exit (/stop or Ctrl+C, exit code 0) ends the loop.
# Uses EvilDonations.exe next to the scripts folder if present, else Python and bot.py.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$exe = Join-Path $root "EvilDonations.exe"
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $root ".venv\Scripts\python.exe" }

while ($true) {
    if (Test-Path $exe) { & $exe } else { & $py bot.py }
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
