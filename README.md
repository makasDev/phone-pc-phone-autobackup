# Phone Auto Backup

A lightweight Windows tray app that watches for one specific Android phone over ADB, indexes photos and videos, and copies new items to your PC using `YYYY/MM/DD` folders.

## What it does

- Watches for a configured Android device serial, so other plugged-in devices are ignored.
- Stays quiet in the background with a tray icon.
- Shows a progress window when the phone is detected or when a backup is started manually.
- Broad mode asks Android MediaStore for indexed photos and videos, which is the same kind of media database Gallery apps use.
- If MediaStore returns nothing, the app falls back to a slower shared-storage scan under `/storage`.
- Remembers transferred files in a small SQLite database.
- Organizes copied files by date: `Destination/YYYY/MM/DD/file.jpg`.
- Avoids overwriting same-name files by adding a short hash suffix when needed.
- Writes a last-run report with copied, skipped, and failed files.
- Copies smaller files first by default, so photos finish quickly while large videos continue afterward.
- Shows average speed, copied-data progress, remaining data, and estimated time left after each completed file.

## Requirements

1. Python 3.10 or newer.
2. Android Platform Tools (`adb.exe`) installed and available on PATH, or set in the app config.
3. USB debugging enabled on the phone.
4. The phone authorized for this PC when Android shows the debugging prompt.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## First run

```powershell
.\.venv\Scripts\python -m phone_auto_backup.app --configure
```

The setup asks for:

- Your phone ADB serial.
- The destination folder on your PC.
- Whether to scan all Gallery-indexed shared storage or only common media folders.

Then start the tray app:

```powershell
.\.venv\Scripts\python -m phone_auto_backup.app
```

## Finding your phone serial

Plug in the phone and run:

```powershell
adb devices
```

Use the value in the first column, for example:

```text
R5CT123ABCD
```

## Config location

The app stores config and transfer memory under:

```text
%APPDATA%\PhoneAutoBackup
```

Important files:

- `config.json`
- `backup_state.sqlite3`
- `phone_auto_backup.log`
- `last_backup_report.txt`

## Scanned formats

Images: `.3fr`, `.ari`, `.arw`, `.avif`, `.bmp`, `.cr2`, `.cr3`, `.crw`, `.dcr`, `.dng`, `.erf`, `.gif`, `.heic`, `.heif`, `.jpg`, `.jpeg`, `.jpe`, `.jxl`, `.k25`, `.kdc`, `.mef`, `.mos`, `.mrw`, `.nef`, `.nrw`, `.orf`, `.png`, `.pef`, `.raw`, `.raf`, `.rw2`, `.rwl`, `.sr2`, `.srf`, `.srw`, `.tif`, `.tiff`, `.webp`, `.x3f`.

Videos: `.3gp`, `.3g2`, `.asf`, `.avi`, `.divx`, `.flv`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mpeg`, `.mpg`, `.mts`, `.ogv`, `.ts`, `.vob`, `.webm`, `.wmv`.

## Notes

The first backup can take a while because the phone has to be indexed. Later runs should be much faster because the app only transfers files it has not already copied.

Broad mode is intentionally MediaStore-first. That means it targets the photos and videos Android has indexed for Gallery-style apps, then only uses the slower filesystem scan if the media database query fails or returns no files.

Video files usually dominate backup time. The app copies smaller files first by default so a long video does not delay thousands of quick photo backups.
