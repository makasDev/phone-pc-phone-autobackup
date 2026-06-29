from __future__ import annotations

import logging
import os
import posixpath
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from .adb import AdbClient, AdbError, VIDEO_EXTENSIONS
from .backup import _format_bytes, _format_duration, _safe_name
from .config import CAMERA_REPORT_PATH, AppConfig
from .state import BackupState

LOGGER = logging.getLogger(__name__)
StatusCallback = Callable[[str], None]

CAMERA_EXTENSIONS = {
    ".cr3", ".dng", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"
}


@dataclass(frozen=True, slots=True)
class CameraFile:
    path: Path
    size: int
    mtime_ns: int


@dataclass(slots=True)
class CameraBackupReport:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    scanned: int = 0
    pushed: list[tuple[Path, str]] = field(default_factory=list)  # (local, remote)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    bytes_to_push: int = 0
    bytes_pushed: int = 0

    def finish(self) -> None:
        self.finished_at = datetime.now()

    def write(self, path: Path = CAMERA_REPORT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        finished = self.finished_at or datetime.now()
        elapsed = max((finished - self.started_at).total_seconds(), 0.001)
        lines = [
            "Camera Auto Backup Report",
            "=" * 25,
            f"Started:  {self.started_at:%Y-%m-%d %H:%M:%S}",
            f"Finished: {finished:%Y-%m-%d %H:%M:%S}",
            f"Scanned:  {self.scanned}",
            f"Pushed:   {len(self.pushed)}",
            f"Skipped:  {len(self.skipped)}",
            f"Failed:   {len(self.failed)}",
            f"Data:     {_format_bytes(self.bytes_pushed)} pushed of {_format_bytes(self.bytes_to_push)}",
            f"Average:  {_format_bytes(self.bytes_pushed / elapsed)}/s",
            "",
            "Pushed files",
            "-" * 12,
        ]
        lines.extend(f"{local} -> {remote}" for local, remote in self.pushed)
        if not self.pushed:
            lines.append("(none)")

        lines.extend(["", "Skipped files", "-" * 13])
        lines.extend(str(local) for local in self.skipped)
        if not self.skipped:
            lines.append("(none)")

        lines.extend(["", "Failed files", "-" * 12])
        lines.extend(f"{local}: {error}" for local, error in self.failed)
        if not self.failed:
            lines.append("(none)")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _find_camera_files(root: Path, include_videos: bool) -> list[CameraFile]:
    files = []
    if not root.exists():
        return files

    extensions = set(CAMERA_EXTENSIONS)
    if include_videos:
        extensions.update(VIDEO_EXTENSIONS)

    # Scan recursively
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue

        # Skip DNG file if a raw CR3 file with the same stem exists in the same folder
        if path.suffix.lower() == ".dng":
            parent = path.parent
            stem = path.stem
            if (parent / f"{stem}.cr3").exists() or (parent / f"{stem}.CR3").exists():
                LOGGER.debug("Skipping DNG file %s as CR3 exists", path)
                continue

        try:
            stat = path.stat()
            files.append(CameraFile(path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns))
        except Exception:
            LOGGER.warning("Could not stat camera file %s", path, exc_info=True)

    return files


def _remote_name(local_file: CameraFile, used_names: set[str]) -> str:
    # If the file is CR3, it will be converted to DNG, so the remote name should have .dng suffix
    original_name = local_file.path.name
    if local_file.path.suffix.lower() == ".cr3":
        original_name = local_file.path.with_suffix(".dng").name

    name = _safe_name(original_name)
    if name not in used_names:
        used_names.add(name)
        return name

    # Handle duplicates by adding a hash
    import hashlib
    digest = hashlib.sha1(str(local_file.path).encode("utf-8")).hexdigest()[:8]
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = _safe_name(f"{stem}-{digest}{suffix}")
    used_names.add(candidate)
    return candidate


class CameraBackupRunner:
    def __init__(self, config: AppConfig, state: BackupState, adb: AdbClient) -> None:
        self.config = config
        self.state = state
        self.adb = adb
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def run_once(self, status: StatusCallback | None = None) -> tuple[int, int]:
        if self._running:
            self._emit(status, "Camera backup is already running.")
            return (0, 0)

        self._running = True
        pushed_count = 0
        skipped_count = 0
        report = CameraBackupReport()

        try:
            source = self.config.camera_source_path
            if not source.exists():
                raise FileNotFoundError(f"Camera source folder does not exist: {source}")

            # Verify target phone is connected
            self._emit(status, "Checking destination phone connection...")
            if not self.adb.is_target_connected():
                raise AdbError(f"Destination phone is not connected (serial: {self.adb.device_serial})")

            # Check DNG Converter path if we have CR3 files to convert
            dng_converter = Path(self.config.dng_converter_path)
            if not self.config.dng_converter_path:
                dng_converter = Path(r"C:\Program Files\Adobe\Adobe DNG Converter\Adobe DNG Converter.exe")

            remote_dir = self.config.to_phone_destination.rstrip("/")
            self._emit(status, "Preparing phone folder...")
            self.adb.ensure_remote_dir(remote_dir)

            self._emit(status, "Indexing camera uploads...")
            local_files = _find_camera_files(source, self.config.include_videos)
            report.scanned = len(local_files)

            pending: list[CameraFile] = []
            for local_file in local_files:
                if self.state.has_pushed_file(
                    self.config.to_phone_device_serial,
                    local_file.path,
                    local_file.size,
                    local_file.mtime_ns,
                ):
                    report.skipped.append(local_file.path)
                else:
                    pending.append(local_file)

            skipped_count = len(report.skipped)
            report.bytes_to_push = sum(local_file.size for local_file in pending)
            bytes_done = 0
            started = time.monotonic()
            used_names: set[str] = set()
            self._emit(status, f"Found {len(local_files)} camera files, {len(pending)} new.")

            def transfer_stats() -> str:
                elapsed = max(time.monotonic() - started, 0.001)
                average_speed = bytes_done / elapsed
                remaining = max(report.bytes_to_push - bytes_done, 0)
                eta = remaining / average_speed if average_speed > 0 else 0
                return (
                    f"{_format_bytes(average_speed)}/s. "
                    f"Progress: {bytes_done / max(report.bytes_to_push, 1) * 100:.1f}%. "
                    f"Remaining: {_format_bytes(remaining)}, ETA {_format_duration(eta)}."
                )

            # Start parallel scanner thread
            scan_queue: queue.Queue[str | None] = queue.Queue()
            def scan_worker() -> None:
                while True:
                    item = scan_queue.get()
                    if item is None:
                        break
                    try:
                        self.adb.scan_remote_file(item)
                    except Exception:
                        LOGGER.exception("Async scan failed for %s", item)
                    scan_queue.task_done()

            scan_thread = threading.Thread(target=scan_worker, name="camera-scan-worker", daemon=True)
            scan_thread.start()

            try:
                for index, local_file in enumerate(pending, start=1):
                    is_cr3 = local_file.path.suffix.lower() == ".cr3"
                    remote_name = _remote_name(local_file, used_names)
                    remote_path = posixpath.join(remote_dir, remote_name)

                    try:
                        # Check free space before copying
                        try:
                            free_space = self.adb.get_remote_free_space_bytes(remote_dir)
                            if free_space < local_file.size:
                                raise AdbError(
                                    f"Phone storage full. Need {_format_bytes(local_file.size)}, "
                                    f"only {_format_bytes(free_space)} free."
                                )
                        except AdbError as exc:
                            if "Phone storage full" in str(exc):
                                raise
                            LOGGER.warning("Could not check free space, continuing transfer: %s", exc)

                        push_source_path = local_file.path

                        if is_cr3:
                            self._emit(
                                status,
                                f"Converting {index}/{len(pending)}: {local_file.path.name} to DNG...",
                            )

                            if not dng_converter.exists():
                                raise FileNotFoundError(
                                    f"Adobe DNG Converter not found at '{dng_converter}'. "
                                    "Please install it or configure the path in settings."
                                )

                            # Run conversion
                            output_dir = str(local_file.path.parent)
                            cmd = [
                                str(dng_converter),
                                "-d", output_dir,
                                str(local_file.path)
                            ]
                            subprocess.run(
                                cmd,
                                capture_output=True,
                                check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                            )

                            # Find DNG file
                            dng_path = local_file.path.with_suffix(".dng")
                            if not dng_path.exists():
                                dng_path = local_file.path.with_suffix(".DNG")
                            
                            if not dng_path.exists():
                                raise FileNotFoundError("Converted DNG file was not created successfully.")

                            push_source_path = dng_path

                        self._emit(
                            status,
                            f"Sending {index}/{len(pending)}: {push_source_path.name} ({_format_bytes(local_file.size)})",
                        )
                        self.adb.push(str(push_source_path), remote_path)
                        scan_queue.put(remote_path)

                        self.state.remember_pushed_file(
                            self.config.to_phone_device_serial,
                            local_file.path,
                            local_file.size,
                            local_file.mtime_ns,
                            remote_path,
                        )

                        report.pushed.append((local_file.path, remote_path))
                        bytes_done += local_file.size
                        report.bytes_pushed = bytes_done
                        pushed_count += 1
                        self._emit(status, transfer_stats(), log=False)

                    except Exception as exc:
                        if isinstance(exc, AdbError) and "Phone storage full" in str(exc):
                            raise
                        LOGGER.exception("Failed to backup camera file %s", local_file.path)
                        report.failed.append((local_file.path, str(exc)))
                        self._emit(status, f"Failed {local_file.path.name}: {exc}")

            finally:
                if pushed_count > 0:
                    self._emit(status, "Finalizing media scanner on phone...")
                scan_queue.put(None)
                scan_thread.join()

            if pushed_count > 0:
                self._emit(status, "Triggering full directory media scan...")
                try:
                    self.adb.scan_remote_directory(remote_dir)
                except Exception:
                    LOGGER.warning("Directory media scan failed, files may need a manual rescan")

            self._emit(
                status,
                f"Camera backup complete. Sent {pushed_count}, skipped {skipped_count}.",
            )
            return (pushed_count, skipped_count)

        except AdbError as exc:
            LOGGER.exception("ADB failure during camera backup")
            self._emit(status, f"ADB error: {exc}")
            return (pushed_count, skipped_count)
        except Exception as exc:
            LOGGER.exception("Camera backup failed")
            self._emit(status, f"Camera backup failed: {exc}")
            return (pushed_count, skipped_count)
        finally:
            report.finish()
            report.write()
            self._running = False

    @staticmethod
    def _emit(status: StatusCallback | None, message: str, log: bool = True) -> None:
        if log:
            LOGGER.info(message)
        if status is not None:
            status(message)
