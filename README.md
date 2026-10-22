# EvilBot (Evil Donations)

Discord bot that logs the daily 50,000 guild donation. Design: `PLAN.md`.

## Run on the PC

1. Install Python and copy this folder to `C:\EvilDonations\`.
2. In that folder: `python -m venv venv`, then `venv\Scripts\pip install -r requirements.txt`.
3. Create `.env` next to `bot.py` (see `.env.example`). Set `DISCORD_TOKEN`, and:
   - `DB_PATH=C:\EvilDonations\donations.db`
   - `BACKUP_DIR=C:\EvilDonations\backups`
   - `ONEDRIVE_BACKUP_DIR=C:\Users\YOU\OneDrive\EvilDonations-backups`
   - `LOG_DIR=C:\EvilDonations\logs`
4. Start it: `powershell -File scripts\run_bot.ps1`.
5. Start at logon: `powershell -File scripts\install_task.ps1`. Also turn on Windows auto-logon so a reboot restarts the bot.

`/stop` (staff) posts the offline notice and ends the bot. Crashes restart after 5 seconds.

## Tests

`venv\Scripts\python -m pytest`

## Build the .exe (on the dev PC)

`.venv\Scripts\pip install pyinstaller`, then `.venv\Scripts\python -m PyInstaller --onefile --name EvilDonations --noconfirm bot.py`. Result: `dist\EvilDonations.exe`.

## Run the .exe on another PC (64-bit Windows, no Python needed)

1. Make a folder, e.g. `C:\EvilDonations\`. Copy in `EvilDonations.exe`, the `scripts` folder, and your `.env`. To keep the data, also copy `donations.db`.
2. Run `powershell -File scriptsun_bot.ps1`, or `scripts\install_task.ps1` to start at logon.
3. Run the bot on **one** PC at a time. Two copies with the same token will both answer.
4. Windows SmartScreen may warn about an unsigned exe: More info, then Run anyway.
