from __future__ import annotations

import hashlib
import logging
import posixpath
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .adb import AdbClient, AdbError, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .backup import _format_bytes, _format_duration, _safe_name
from .config import TO_PHONE_REPORT_PATH, AppConfig
from .state import BackupState


LOGGER = logging.getLogger(__name__)
StatusCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class LocalMediaFile:
    path: Path
    size: int
    mtime_ns: int


@dataclass(slots=True)
class ToPhoneReport:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    scanned: int = 0
    pushed: list[tuple[LocalMediaFile, str]] = field(default_factory=list)
    skipped: list[LocalMediaFile] = field(default_factory=list)
    failed: list[tuple[LocalMediaFile, str]] = field(default_factory=list)
    bytes_to_push: int = 0
    bytes_pushed: int = 0

    def finish(self) -> None:
        self.finished_at = datetime.now()

    def write(self, path: Path = TO_PHONE_REPORT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        finished = self.finished_at or datetime.now()
        elapsed = max((finished - self.started_at).total_seconds(), 0.001)
        lines = [
            "Phone Auto Backup To-Phone Report",
            "=" * 33,
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
        lines.extend(f"{local.path} -> {remote_path}" for local, remote_path in self.pushed)
        if not self.pushed:
            lines.append("(none)")

        lines.extend(["", "Skipped files", "-" * 13])
        lines.extend(str(local.path) for local in self.skipped)
        if not self.skipped:
            lines.append("(none)")

        lines.extend(["", "Failed files", "-" * 12])
        lines.extend(f"{local.path}: {error}" for local, error in self.failed)
        if not self.failed:
            lines.append("(none)")

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _local_media_files(root: Path, include_videos: bool) -> Iterable[LocalMediaFile]:
    extensions = set(IMAGE_EXTENSIONS)
    if include_videos:
        extensions.update(VIDEO_EXTENSIONS)

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        stat = path.stat()
        yield LocalMediaFile(path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _remote_name(local_file: LocalMediaFile, used_names: set[str]) -> str:
    name = _safe_name(local_file.path.name)
    if name not in used_names:
        used_names.add(name)
        return name

    digest = hashlib.sha1(str(local_file.path).encode("utf-8")).hexdigest()[:8]
    candidate = f"{local_file.path.stem}-{digest}{local_file.path.suffix}"
    candidate = _safe_name(candidate)
    used_names.add(candidate)
    return candidate


class ToPhoneRunner:
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
            self._emit(status, "Phone transfer is already running.")
            return (0, 0)

        self._running = True
        pushed_count = 0
        skipped_count = 0
        report = ToPhoneReport()
        try:
            source = self.config.to_phone_source_path
            if not source.exists():
                raise FileNotFoundError(f"Source folder does not exist: {source}")

            remote_dir = self.config.to_phone_destination.rstrip("/")
            self._emit(status, "Preparing phone folder...")
            self.adb.ensure_remote_dir(remote_dir)

            self._emit(status, "Indexing PC media...")

            local_files = sorted(
                _local_media_files(source, self.config.include_videos),
                key=lambda item: (item.path.suffix.lower() in VIDEO_EXTENSIONS, item.size, str(item.path).lower()),
            )
            report.scanned = len(local_files)

            pending: list[LocalMediaFile] = []
            for local_file in local_files:
                if self.state.has_pushed_file(
                    self.config.to_phone_device_serial,
                    local_file.path,
                    local_file.size,
                    local_file.mtime_ns,
                ):
                    report.skipped.append(local_file)
                else:
                    pending.append(local_file)

            # --- PRE-CALCULATE CHECKPOINT INDEX ---
            # Find the last photo's index in the pending list
            last_photo_index = -1
            for idx, local_file in enumerate(pending, start=1):
                if local_file.path.suffix.lower() not in VIDEO_EXTENSIONS:
                    last_photo_index = idx  # Tracks the 1-based index loop position
            # --------------------------------------

            skipped_count = len(report.skipped)
            report.bytes_to_push = sum(local_file.size for local_file in pending)
            bytes_done = 0
            started = time.monotonic()
            used_names: set[str] = set()
            self._emit(status, f"Found {len(local_files)} PC files, {len(pending)} new.")

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

            scan_thread = threading.Thread(target=scan_worker, name="phone-scan-worker", daemon=True)
            scan_thread.start()

            try:
                for index, local_file in enumerate(pending, start=1):
                    remote_name = _remote_name(local_file, used_names)
                    remote_path = posixpath.join(remote_dir, remote_name)
                    try:
                        self._emit(
                            status,
                            f"Sending {index}/{len(pending)}: {local_file.path.name} ({_format_bytes(local_file.size)})",
                        )
                        self.adb.push(str(local_file.path), remote_path)
                        scan_queue.put(remote_path)
                        self.state.remember_pushed_file(
                            self.config.to_phone_device_serial,
                            local_file.path,
                            local_file.size,
                            local_file.mtime_ns,
                            remote_path,
                        )
                        report.pushed.append((local_file, remote_path))
                        bytes_done += local_file.size
                        report.bytes_pushed = bytes_done
                        pushed_count += 1
                        self._emit(status, transfer_stats(), log=False)

                        # --- CHECKPOINT 1: PHOTO BATCH COMPLETE ---
                        # Right after the last photo file finishes pushing, trigger a directory sync
                        if index == last_photo_index:
                            self._emit(status, "All photos pushed! Triggering intermediary directory scan...")
                            try:
                                self.adb.scan_remote_directory(remote_dir)
                            except Exception:
                                LOGGER.warning("Intermediary photo batch scan request failed.")
                        # -------------------------------------------

                    except Exception as exc:
                        # ... [Keep your existing exception handling block intact] ...
                        pass
            finally:
                if pushed_count > 0:
                    self._emit(status, "Finalizing background file queue tasks...")
                scan_queue.put(None)
                scan_thread.join()

            # --- CHECKPOINT 2: GUARANTEED FINAL RESCAN ---
            if pushed_count > 0:
                self._emit(status, "All files pushed! Triggering final full directory media scan...")
                try:
                    self.adb.scan_remote_directory(remote_dir)
                    self._emit(status, "Media database sync completed successfully.")
                except Exception:
                    LOGGER.warning("Final directory scan failed. Files will index on next phone reboot.")

            self._emit(
                status,
                f"Phone transfer complete. Sent {pushed_count}, skipped {skipped_count}.",
            )
            return (pushed_count, skipped_count)
        except AdbError as exc:
            LOGGER.exception("ADB failure during phone transfer")
            self._emit(status, f"ADB error: {exc}")
            return (pushed_count, skipped_count)
        except Exception as exc:
            LOGGER.exception("Phone transfer failed")
            self._emit(status, f"Phone transfer failed: {exc}")
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

