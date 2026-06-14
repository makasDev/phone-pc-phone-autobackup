from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .adb import AdbClient, AdbError, VIDEO_EXTENSIONS
from .config import REPORT_PATH, AppConfig
from .state import BackupState, RemoteFile


LOGGER = logging.getLogger(__name__)
StatusCallback = Callable[[str], None]


@dataclass(slots=True)
class BackupReport:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    scanned: int = 0
    copied: list[tuple[RemoteFile, Path]] = field(default_factory=list)
    skipped: list[RemoteFile] = field(default_factory=list)
    failed: list[tuple[RemoteFile, str]] = field(default_factory=list)
    bytes_to_copy: int = 0
    bytes_copied: int = 0

    def finish(self) -> None:
        self.finished_at = datetime.now()

    def write(self, path: Path = REPORT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        finished = self.finished_at or datetime.now()
        lines = [
            "Phone Auto Backup Report",
            "=" * 24,
            f"Started:  {self.started_at:%Y-%m-%d %H:%M:%S}",
            f"Finished: {finished:%Y-%m-%d %H:%M:%S}",
            f"Scanned:  {self.scanned}",
            f"Copied:   {len(self.copied)}",
            f"Skipped:  {len(self.skipped)}",
            f"Failed:   {len(self.failed)}",
            f"Data:     {_format_bytes(self.bytes_copied)} copied of {_format_bytes(self.bytes_to_copy)}",
            f"Average:  {_format_bytes(self.bytes_copied / max((finished - self.started_at).total_seconds(), 0.001))}/s",
            "",
            "Copied files",
            "-" * 12,
        ]
        lines.extend(
            f"{remote.path} -> {local_path}" for remote, local_path in self.copied
        )
        if not self.copied:
            lines.append("(none)")

        lines.extend(["", "Skipped files", "-" * 13])
        lines.extend(remote.path for remote in self.skipped)
        if not self.skipped:
            lines.append("(none)")

        lines.extend(["", "Failed files", "-" * 12])
        lines.extend(f"{remote.path}: {error}" for remote, error in self.failed)
        if not self.failed:
            lines.append("(none)")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_name(name: str) -> str:
    forbidden = '<>:"/\\|?*'
    cleaned = "".join("_" if char in forbidden or ord(char) < 32 else char for char in name)
    cleaned = cleaned.strip(" .")
    return cleaned or "unnamed"


def _dated_folder(destination: Path, remote_file: RemoteFile) -> Path:
    date = datetime.fromtimestamp(remote_file.mtime)
    return destination / f"{date:%Y}" / f"{date:%m}" / f"{date:%d}"


def _unique_local_path(folder: Path, remote_file: RemoteFile) -> Path:
    original = Path(remote_file.path).name
    name = _safe_name(original)
    candidate = folder / name
    if not candidate.exists():
        return candidate

    digest = hashlib.sha1(remote_file.path.encode("utf-8")).hexdigest()[:8]
    stem = candidate.stem
    suffix = candidate.suffix
    return folder / f"{stem}-{digest}{suffix}"


def _format_bytes(size: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _is_video(remote_file: RemoteFile) -> bool:
    return Path(remote_file.path).suffix.lower() in VIDEO_EXTENSIONS


class BackupRunner:
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
            self._emit(status, "Backup is already running.")
            return (0, 0)

        self._running = True
        transferred = 0
        skipped = 0
        report = BackupReport()
        try:
            destination = self.config.destination_path
            destination.mkdir(parents=True, exist_ok=True)

            self._emit(status, "Indexing phone media...")
            remote_files = self.adb.list_media_files()
            report.scanned = len(remote_files)
            pending: list[RemoteFile] = []
            for remote_file in remote_files:
                if self.state.has_file(self.config.device_serial, remote_file):
                    report.skipped.append(remote_file)
                else:
                    pending.append(remote_file)

            skipped = len(report.skipped)
            if self.config.copy_smaller_files_first:
                pending.sort(key=lambda item: (_is_video(item), item.size, item.path.lower()))

            report.bytes_to_copy = sum(remote_file.size for remote_file in pending)
            bytes_done = 0
            started = time.monotonic()
            self._emit(status, f"Found {len(remote_files)} files, {len(pending)} new.")

            def transfer_stats() -> str:
                elapsed = max(time.monotonic() - started, 0.001)
                average_speed = bytes_done / elapsed
                remaining = max(report.bytes_to_copy - bytes_done, 0)
                eta = remaining / average_speed if average_speed > 0 else 0
                return (
                    f"{_format_bytes(average_speed)}/s. "
                    f"Progress: {bytes_done / max(report.bytes_to_copy, 1) * 100:.1f}%. "
                    f"Remaining: {_format_bytes(remaining)}, ETA {_format_duration(eta)}."
                )

            for index, remote_file in enumerate(pending, start=1):
                try:
                    folder = _dated_folder(destination, remote_file)
                    folder.mkdir(parents=True, exist_ok=True)
                    final_path = _unique_local_path(folder, remote_file)
                    self._emit(
                        status,
                        (
                            f"Copying {index}/{len(pending)}: {Path(remote_file.path).name} "
                            f"({_format_bytes(remote_file.size)})"
                        ),
                    )

                    with tempfile.TemporaryDirectory(prefix="phone-backup-") as tmp_dir:
                        temp_target = Path(tmp_dir) / final_path.name
                        self.adb.pull(remote_file, str(temp_target))
                        shutil.move(str(temp_target), final_path)

                    self.state.remember_file(self.config.device_serial, remote_file, final_path)
                    report.copied.append((remote_file, final_path))
                    bytes_done += remote_file.size
                    report.bytes_copied = bytes_done
                    transferred += 1
                    self._emit(status, transfer_stats(), log=False)
                except Exception as exc:
                    LOGGER.exception("Failed to copy %s", remote_file.path)
                    report.failed.append((remote_file, str(exc)))
                    self._emit(status, f"Failed {Path(remote_file.path).name}: {exc}")

            self._emit(status, f"Backup complete. Copied {transferred}, skipped {skipped}.")
            return (transferred, skipped)
        except AdbError as exc:
            LOGGER.exception("ADB failure during backup")
            self._emit(status, f"ADB error: {exc}")
            return (transferred, skipped)
        except Exception as exc:
            LOGGER.exception("Backup failed")
            self._emit(status, f"Backup failed: {exc}")
            return (transferred, skipped)
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
