from __future__ import annotations

import argparse
import logging
import shutil
import sys
from tkinter import Tk, filedialog, messagebox, simpledialog

from .adb import AdbClient
from .backup import BackupRunner
from .to_phone import ToPhoneRunner
from .camera_backup import CameraBackupRunner
from .config import CONFIG_PATH, DB_PATH, LOG_PATH, AppConfig, load_config, save_config
from .state import BackupState


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def configure_interactive() -> AppConfig:
    config = load_config()

    root = Tk()
    root.withdraw()

    adb_default = shutil.which("adb") or config.adb_path
    adb_path = simpledialog.askstring(
        "Phone Auto Backup",
        "ADB path or command:",
        initialvalue=adb_default,
    )
    if adb_path:
        config.adb_path = adb_path.strip()

    serial = simpledialog.askstring(
        "Phone Auto Backup",
        "Source Phone ADB serial (for backups):",
        initialvalue=config.device_serial,
    )
    if serial:
        config.device_serial = serial.strip()

    to_phone_serial = simpledialog.askstring(
        "Phone Auto Backup",
        "Destination Phone ADB serial (for transfers):",
        initialvalue=config.to_phone_device_serial,
    )
    if to_phone_serial:
        config.to_phone_device_serial = to_phone_serial.strip()

    destination = filedialog.askdirectory(
        title="Choose backup destination",
        initialdir=str(config.destination_path),
    )
    if destination:
        config.destination = destination

    scan_all = messagebox.askyesno(
        "Phone Auto Backup",
        "Scan all shared phone storage?\n\nChoose Yes for the broadest Samsung Gallery-style backup.",
    )
    config.scan_all_shared_storage = bool(scan_all)

    include_videos = messagebox.askyesno(
        "Phone Auto Backup",
        "Include videos too?",
    )
    config.include_videos = bool(include_videos)

    to_phone_src = filedialog.askdirectory(
        title="Choose transfer to phone source folder",
        initialdir=str(config.to_phone_source_path),
    )
    if to_phone_src:
        config.to_phone_source = to_phone_src

    to_phone_dst = simpledialog.askstring(
        "Phone Auto Backup",
        "Android destination folder for transfers:",
        initialvalue=config.to_phone_destination,
    )
    if to_phone_dst:
        config.to_phone_destination = to_phone_dst.strip()

    camera_src = filedialog.askdirectory(
        title="Choose camera uploads source folder",
        initialdir=str(config.camera_source_path),
    )
    if camera_src:
        config.camera_source = camera_src

    dng_conv = filedialog.askopenfilename(
        title="Choose Adobe DNG Converter executable",
        initialfile="Adobe DNG Converter.exe",
        filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")],
    )
    if dng_conv:
        config.dng_converter_path = dng_conv

    save_config(config)
    messagebox.showinfo("Phone Auto Backup", f"Saved config:\n{CONFIG_PATH}")
    root.destroy()
    return config


def run_console_backup(config: AppConfig) -> int:
    state = BackupState(DB_PATH)
    try:
        runner = BackupRunner(config, state, AdbClient(config))
        runner.run_once(print)
        return 0
    finally:
        state.close()


def run_console_to_phone(config: AppConfig) -> int:
    state = BackupState(DB_PATH)
    try:
        runner = ToPhoneRunner(config, state, AdbClient(config, config.to_phone_device_serial))
        runner.run_once(print)
        return 0
    finally:
        state.close()


def run_console_camera(config: AppConfig) -> int:
    state = BackupState(DB_PATH)
    try:
        runner = CameraBackupRunner(config, state, AdbClient(config, config.to_phone_device_serial))
        runner.run_once(print)
        return 0
    finally:
        state.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-back up Android photos over ADB.")
    parser.add_argument("--configure", action="store_true", help="Open first-run configuration dialogs.")
    parser.add_argument("--once", action="store_true", help="Run one backup in the console and exit.")
    parser.add_argument("--to-phone", action="store_true", help="Run one transfer to phone in the console and exit.")
    parser.add_argument("--camera", action="store_true", help="Run one camera backup in the console and exit.")
    parser.add_argument("--config", action="store_true", help="Print the active config path and exit.")
    args = parser.parse_args(argv)

    configure_logging()

    if args.config:
        print(CONFIG_PATH)
        return 0

    if args.configure:
        configure_interactive()
        return 0

    config = load_config()
    if not config.device_serial:
        print("No phone serial is configured yet. Run with --configure first.")
        print(f"Config path: {CONFIG_PATH}")
        return 2

    if args.once:
        return run_console_backup(config)

    if args.to_phone:
        if not config.to_phone_device_serial:
            print("No destination phone serial is configured yet. Run with --configure first.")
            print(f"Config path: {CONFIG_PATH}")
            return 2
        return run_console_to_phone(config)

    if args.camera:
        if not config.to_phone_device_serial:
            print("No destination phone serial is configured yet. Run with --configure first.")
            print(f"Config path: {CONFIG_PATH}")
            return 2
        return run_console_camera(config)

    from .tray import TrayApp

    state = BackupState(DB_PATH)
    TrayApp(config, state).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
