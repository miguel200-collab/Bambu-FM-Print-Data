@echo off
:: run_backup.bat — one backup run (called nightly by Task Scheduler, or by hand).
:: Destination comes from "backup.dest" in config.json (point it at a folder that
:: OneDrive / Google Drive syncs so the copy leaves the laptop).

cd /d "%~dp0"
python analysis\backup_db.py >> backup.log 2>&1
