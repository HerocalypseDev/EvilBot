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
