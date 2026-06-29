from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


APP_NAME = "PhoneAutoBackup"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "backup_state.sqlite3"
LOG_PATH = APP_DIR / "phone_auto_backup.log"
REPORT_PATH = APP_DIR / "last_backup_report.txt"
TO_PHONE_REPORT_PATH = APP_DIR / "last_to_phone_report.txt"
CAMERA_REPORT_PATH = APP_DIR / "last_camera_backup_report.txt"


@dataclass(slots=True)
class AppConfig:
    device_serial: str = ""
    to_phone_device_serial: str = ""
    destination: str = str(Path.home() / "Pictures" / "Phone Backup")
    to_phone_source: str = str(Path.home() / "Pictures" / "Phone Backup")
    to_phone_destination: str = "/sdcard/Pictures/Phone Auto Backup"
    camera_source: str = str(Path.home() / "Pictures" / "Camera Uploads")
    dng_converter_path: str = r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe"
    adb_path: str = "adb"
    poll_seconds: int = 10
    media_query_timeout_seconds: int = 120
    filesystem_scan_timeout_seconds: int = 900
    pull_timeout_seconds: int = 1800
    scan_all_shared_storage: bool = True
    include_videos: bool = True
    copy_smaller_files_first: bool = True
    media_roots: list[str] = field(
        default_factory=lambda: [
            "/sdcard/DCIM",
            "/sdcard/Pictures",
            "/sdcard/Download",
            "/sdcard/Movies",
            "/sdcard/Instagram",
            "/sdcard/Snapchat",
            "/sdcard/WhatsApp/Media",
            "/sdcard/Telegram",
        ]
    )

    @property
    def destination_path(self) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(self.destination)))

    @property
    def to_phone_source_path(self) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(self.to_phone_source)))

    @property
    def camera_source_path(self) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(self.camera_source)))


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    ensure_app_dir()
    if not CONFIG_PATH.exists():
        config = AppConfig()
        save_config(config)
        return config

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    defaults = asdict(AppConfig())
    defaults.update(raw)
    return AppConfig(**defaults)


def save_config(config: AppConfig) -> None:
    ensure_app_dir()
    with CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(asdict(config), handle, indent=2)
        handle.write("\n")
