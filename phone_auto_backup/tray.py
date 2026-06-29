from __future__ import annotations

import logging
import threading
import webbrowser

from PIL import Image, ImageDraw
import pystray

from .adb import AdbClient, AdbError
from .backup import BackupRunner
from .to_phone import ToPhoneRunner
from .camera_backup import CameraBackupRunner
from .config import CONFIG_PATH, LOG_PATH, REPORT_PATH, TO_PHONE_REPORT_PATH, CAMERA_REPORT_PATH, AppConfig
from .state import BackupState
from .ui import BackupWindow


LOGGER = logging.getLogger(__name__)


class TrayApp:
    def __init__(self, config: AppConfig, state: BackupState) -> None:
        self.config = config
        self.state = state
        self.adb = AdbClient(config, config.device_serial)
        self.to_phone_adb = AdbClient(config, config.to_phone_device_serial)
        self.runner = BackupRunner(config, state, self.adb)
        self.to_phone_runner = ToPhoneRunner(config, state, self.to_phone_adb)
        self.camera_runner = CameraBackupRunner(config, state, self.to_phone_adb)
        self.window = BackupWindow()
        self.stop_event = threading.Event()
        self.status = "Waiting for phone..."
        self.auto_backup_enabled = True
        self.icon = pystray.Icon(
            "Phone Auto Backup",
            self._make_icon("idle"),
            "Phone Auto Backup",
            self._build_menu(),
        )

    def run(self) -> int:
        watcher = threading.Thread(target=self._watch_loop, name="phone-watch", daemon=True)
        watcher.start()
        self.icon.run_detached()
        self.window.run()
        return 0

    def _build_menu(self) -> pystray.Menu:
        is_idle = lambda _: not self.runner.running and not self.to_phone_runner.running and not self.camera_runner.running
        return pystray.Menu(
            pystray.MenuItem(lambda _: self.status, None, enabled=False),
            pystray.MenuItem("Auto-backup enabled", self._toggle_auto_backup, checked=lambda _: self.auto_backup_enabled),
            pystray.MenuItem("Back up now", self._backup_now, enabled=is_idle),
            pystray.MenuItem("Transfer to phone now", self._to_phone_now, enabled=is_idle),
            pystray.MenuItem("Back up camera now", self._backup_camera_now, enabled=is_idle),
            pystray.MenuItem("Show progress", self._show_progress),
            pystray.MenuItem("Open destination", self._open_destination),
            pystray.MenuItem("Open last report", self._open_report),
            pystray.MenuItem("Open last to-phone report", self._open_to_phone_report),
            pystray.MenuItem("Open last camera report", self._open_camera_report),
            pystray.MenuItem("Open config", self._open_config),
            pystray.MenuItem("Open log", self._open_log),
            pystray.MenuItem("Quit", self._quit),
        )

    def _toggle_auto_backup(self, _: object) -> None:
        self.auto_backup_enabled = not self.auto_backup_enabled
        self.icon.update_menu()

    def _watch_loop(self) -> None:
        was_connected = False
        was_to_phone_connected = False
        while not self.stop_event.is_set():
            try:
                serials = self.adb.connected_serials()

                source_serial = self.config.device_serial.strip()
                source_connected = source_serial in serials if source_serial else False

                dest_serial = self.config.to_phone_device_serial.strip()
                dest_connected = dest_serial in serials if dest_serial else False

                if source_connected and not was_connected:
                    if self.auto_backup_enabled and not self.runner.running and not self.to_phone_runner.running:
                        self._set_status("Source phone detected. Starting backup...")
                        self.window.show()
                        self._start_backup_thread()

                if dest_connected and not was_to_phone_connected:
                    if not self.runner.running and not self.to_phone_runner.running:
                        self._set_status("Destination phone detected. Starting transfer to phone...")
                        self.window.show()
                        self._start_to_phone_thread()

                if self.runner.running:
                    pass
                elif self.to_phone_runner.running:
                    pass
                elif source_connected and dest_connected:
                    self._set_status("Both phones connected.")
                elif source_connected:
                    self._set_status("Source phone connected.")
                elif dest_connected:
                    self._set_status("Destination phone connected.")
                else:
                    self._set_status("Waiting for phone...")

                was_connected = source_connected
                was_to_phone_connected = dest_connected
            except AdbError as exc:
                self._set_status(f"ADB error: {exc}")
                LOGGER.warning("ADB check failed: %s", exc)

            self.stop_event.wait(self.config.poll_seconds)

    def _start_backup_thread(self) -> None:
        if self.runner.running:
            return
        thread = threading.Thread(
            target=self._run_backup_with_chain,
            name="phone-backup",
            daemon=True,
        )
        thread.start()

    def _run_backup_with_chain(self) -> None:
        self.runner.run_once(status=self._set_status)
        try:
            # If destination phone is connected, automatically start the transfer to phone!
            serials = self.adb.connected_serials()
            dest_serial = self.config.to_phone_device_serial.strip()
            if dest_serial and dest_serial in serials:
                self._set_status("Backup finished. Automatically starting transfer to receiver phone...")
                self._start_to_phone_thread()
        except Exception as exc:
            LOGGER.exception("Failed to check and start chained transfer")

    def _start_to_phone_thread(self) -> None:
        if self.to_phone_runner.running or self.runner.running or self.camera_runner.running:
            return
        thread = threading.Thread(
            target=self.to_phone_runner.run_once,
            kwargs={"status": self._set_status},
            name="phone-transfer",
            daemon=True,
        )
        thread.start()

    def _start_camera_thread(self) -> None:
        if self.camera_runner.running or self.runner.running or self.to_phone_runner.running:
            return
        thread = threading.Thread(
            target=self.camera_runner.run_once,
            kwargs={"status": self._set_status},
            name="camera-backup",
            daemon=True,
        )
        thread.start()

    def _backup_now(self, _: object) -> None:
        self.window.show()
        self._start_backup_thread()

    def _to_phone_now(self, _: object) -> None:
        self.window.show()
        self._start_to_phone_thread()

    def _backup_camera_now(self, _: object) -> None:
        self.window.show()
        self._start_camera_thread()

    def _show_progress(self, _: object) -> None:
        self.window.show()

    def _open_destination(self, _: object) -> None:
        path = self.config.destination_path
        path.mkdir(parents=True, exist_ok=True)
        webbrowser.open(path.as_uri())

    def _open_config(self, _: object) -> None:
        webbrowser.open(CONFIG_PATH.as_uri())

    def _open_log(self, _: object) -> None:
        LOG_PATH.touch(exist_ok=True)
        webbrowser.open(LOG_PATH.as_uri())

    def _open_report(self, _: object) -> None:
        REPORT_PATH.touch(exist_ok=True)
        webbrowser.open(REPORT_PATH.as_uri())

    def _open_to_phone_report(self, _: object) -> None:
        TO_PHONE_REPORT_PATH.touch(exist_ok=True)
        webbrowser.open(TO_PHONE_REPORT_PATH.as_uri())

    def _open_camera_report(self, _: object) -> None:
        CAMERA_REPORT_PATH.touch(exist_ok=True)
        webbrowser.open(CAMERA_REPORT_PATH.as_uri())

    def _quit(self, _: object) -> None:
        self.stop_event.set()
        self.state.close()
        self.icon.stop()
        self.window.stop()

    def _set_status(self, message: str) -> None:
        self.status = message
        self.window.update_status(message)
        self.icon.title = f"Phone Auto Backup - {message[:80]}"
        is_busy = self.runner.running or self.to_phone_runner.running or self.camera_runner.running
        self.icon.icon = self._make_icon("busy" if is_busy else "idle")
        self.icon.update_menu()

    @staticmethod
    def _make_icon(mode: str) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = (64, 130, 220, 255) if mode == "idle" else (30, 160, 90, 255)
        draw.rounded_rectangle((18, 6, 46, 58), radius=6, fill=color)
        draw.rectangle((25, 10, 39, 13), fill=(255, 255, 255, 180))
        draw.ellipse((29, 48, 35, 54), fill=(255, 255, 255, 220))
        return image
