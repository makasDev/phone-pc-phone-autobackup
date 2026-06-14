from __future__ import annotations

import logging
import posixpath
import re
import shlex
import subprocess
import urllib.parse
from collections.abc import Iterable

from .config import AppConfig
from .state import RemoteFile


LOGGER = logging.getLogger(__name__)
MEDIASTORE_ROW = re.compile(
    r"_data=(?P<path>.*?), _size=(?P<size>\d+), date_modified=(?P<mtime>\d+)"
)

IMAGE_EXTENSIONS = {
    ".3fr",
    ".ari",
    ".arw",
    ".avif",
    ".bmp",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".gif",
    ".heic",
    ".heif",
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jxl",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".png",
    ".pef",
    ".raw",
    ".raf",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".tif",
    ".tiff",
    ".webp",
    ".x3f",
}

VIDEO_EXTENSIONS = {
    ".3gp",
    ".3g2",
    ".asf",
    ".avi",
    ".divx",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogv",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, config: AppConfig, device_serial: str | None = None) -> None:
        self.config = config
        self._device_serial = device_serial

    @property
    def device_serial(self) -> str:
        return self._device_serial if self._device_serial is not None else self.config.device_serial

    def _run(self, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        command = [self.config.adb_path, *args]
        LOGGER.debug("Running ADB command: %s", command)
        try:
            return subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"Could not find adb executable: {self.config.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB command timed out") from exc

    def connected_serials(self) -> set[str]:
        result = self._run(["devices"], timeout=10)
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or "adb devices failed")

        serials: set[str] = set()
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.add(parts[0])
        return serials

    def is_target_connected(self) -> bool:
        serial = self.device_serial.strip()
        if not serial:
            return False
        return serial in self.connected_serials()

    def list_media_files(self) -> list[RemoteFile]:
        extensions = set(IMAGE_EXTENSIONS)
        if self.config.include_videos:
            extensions.update(VIDEO_EXTENSIONS)

        if self.config.scan_all_shared_storage:
            media_store_files = self._query_media_store(extensions)
            if media_store_files:
                return sorted(media_store_files, key=lambda item: item.path.lower())
            LOGGER.warning("MediaStore query returned no files; falling back to filesystem scan")

        roots = ["/storage"] if self.config.scan_all_shared_storage else self.config.media_roots
        files: dict[str, RemoteFile] = {}
        for root in roots:
            for remote_file in self._find_files(root, extensions):
                files[remote_file.path] = remote_file
        return sorted(files.values(), key=lambda item: item.path.lower())

    def pull(self, remote_file: RemoteFile, local_path: str) -> None:
        result = self._run(
            ["-s", self.device_serial, "pull", remote_file.path, local_path],
            timeout=self.config.pull_timeout_seconds,
        )
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or f"Failed to pull {remote_file.path}")

    def push(self, local_path: str, remote_path: str) -> None:
        result = self._run(
            ["-s", self.device_serial, "push", local_path, remote_path],
            timeout=self.config.pull_timeout_seconds,
        )
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or f"Failed to push {local_path}")

    def ensure_remote_dir(self, remote_dir: str) -> None:
        result = self._run(
            ["-s", self.device_serial, "shell", "mkdir", "-p", shlex.quote(remote_dir)],
            timeout=30,
        )
        if result.returncode != 0:
            raise AdbError(result.stderr.strip() or f"Failed to create {remote_dir}")

    def scan_remote_file(self, remote_path: str) -> None:
        """Forwards an explicit file path index update directly to the MediaProvider service."""
        # Using the direct modern 'media scan-file' system call instead of a broadcast intent
        self._run(
            [
                "-s",
                self.device_serial,
                "shell",
                "media",
                "scan-file",
                shlex.quote(remote_path),
            ],
            timeout=30,
        )

    def scan_remote_directory(self, remote_dir: str) -> None:
        """Forces Android's MediaProvider service to run an immediate, complete scan 
        on the external primary storage volume to catalog newly added files.
        """
        # Modern alternative to MEDIA_MOUNTED. This triggers a lightning-fast native internal volume scan.
        # It updates the central MediaStore indexing tables instantly.
        self._run(
            [
                "-s",
                self.device_serial,
                "shell",
                "content",
                "call",
                "--method",
                "scan_volume",
                "--uri",
                "content://media",
                "--arg",
                "external_primary",
            ],
            timeout=60,
        )

    def _query_media_store(self, extensions: set[str]) -> list[RemoteFile]:
        uris = ["content://media/external/images/media"]
        if self.config.include_videos:
            uris.append("content://media/external/video/media")

        files: dict[str, RemoteFile] = {}
        for uri in uris:
            result = self._run(
                [
                    "-s",
                    self.device_serial,
                    "shell",
                    "content",
                    "query",
                    "--uri",
                    uri,
                    "--projection",
                    "_data:_size:date_modified",
                ],
                timeout=self.config.media_query_timeout_seconds,
            )
            if result.returncode != 0:
                LOGGER.warning("MediaStore query failed for %s: %s", uri, result.stderr.strip())
                continue

            for remote_file in self._parse_media_store_output(result.stdout, extensions):
                files[remote_file.path] = remote_file

        return list(files.values())

    def _find_files(self, root: str, extensions: Iterable[str]) -> list[RemoteFile]:
        predicates = " -o ".join(
            f"-iname {shlex.quote('*' + extension)}" for extension in sorted(extensions)
        )
        quoted_root = shlex.quote(root)
        script = (
            f"if [ -d {quoted_root} ]; then "
            f"find {quoted_root} -type f \\( {predicates} \\) -print0 2>/dev/null "
            "| xargs -0 stat -c '%s|%Y|%n' 2>/dev/null; "
            "fi"
        )
        result = self._run(
            ["-s", self.device_serial, "shell", script],
            timeout=self.config.filesystem_scan_timeout_seconds,
        )
        if result.returncode != 0:
            LOGGER.warning("Failed indexing %s: %s", root, result.stderr.strip())
            return []

        return list(self._parse_stat_output(result.stdout))

    def get_remote_free_space_bytes(self, path: str = "/sdcard") -> int:
        result = self._run(["-s", self.device_serial, "shell", "df", "-k", shlex.quote(path)], timeout=10)
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[-1].split()
                headers = lines[0].lower().split()
                avail_idx = -1
                for idx, h in enumerate(headers):
                    if "avail" in h or "free" in h:
                        avail_idx = idx
                        break
                if avail_idx != -1 and len(parts) > avail_idx:
                    try:
                        return int(parts[avail_idx]) * 1024
                    except ValueError:
                        pass

        # Fallback to stat -f
        result = self._run(["-s", self.device_serial, "shell", "stat", "-f", "-c", "%a %S", shlex.quote(path)], timeout=10)
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                try:
                    free_blocks = int(parts[0])
                    block_size = int(parts[1])
                    return free_blocks * block_size
                except ValueError:
                    pass

        raise AdbError("Could not retrieve remote storage free space")

    @staticmethod
    def _parse_media_store_output(output: str, extensions: set[str]) -> Iterable[RemoteFile]:
        for line in output.splitlines():
            match = MEDIASTORE_ROW.search(line)
            if not match:
                continue

            path = posixpath.normpath(match.group("path").strip())
            if not path or path == ".":
                continue
            if posixpath.splitext(path)[1].lower() not in extensions:
                continue

            try:
                size = int(match.group("size"))
                mtime = int(match.group("mtime"))
            except ValueError:
                continue

            yield RemoteFile(path=path, size=size, mtime=mtime)

    @staticmethod
    def _parse_stat_output(output: str) -> Iterable[RemoteFile]:
        for line in output.splitlines():
            size_text, separator, rest = line.partition("|")
            if not separator:
                continue
            mtime_text, separator, path = rest.partition("|")
            if not separator or not path:
                continue
            try:
                size = int(size_text)
                mtime = int(mtime_text)
            except ValueError:
                continue

            normalized = posixpath.normpath(path.strip())
            if normalized.startswith("/sdcard/Android/data/"):
                continue
            yield RemoteFile(path=normalized, size=size, mtime=mtime)
