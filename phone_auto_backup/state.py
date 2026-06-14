from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS transferred_files (
    device_serial TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    remote_size INTEGER NOT NULL,
    remote_mtime INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    transferred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (device_serial, remote_path, remote_size, remote_mtime)
);

CREATE INDEX IF NOT EXISTS idx_transferred_device_path
ON transferred_files (device_serial, remote_path);

CREATE TABLE IF NOT EXISTS pushed_files (
    device_serial TEXT NOT NULL,
    local_path TEXT NOT NULL,
    local_size INTEGER NOT NULL,
    local_mtime_ns INTEGER NOT NULL,
    remote_path TEXT NOT NULL,
    pushed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (device_serial, local_path, local_size, local_mtime_ns)
);

CREATE INDEX IF NOT EXISTS idx_pushed_device_path
ON pushed_files (device_serial, local_path);
"""


@dataclass(frozen=True, slots=True)
class RemoteFile:
    path: str
    size: int
    mtime: int


class BackupState:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def has_file(self, device_serial: str, remote_file: RemoteFile) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM transferred_files
            WHERE device_serial = ?
              AND remote_path = ?
              AND remote_size = ?
              AND remote_mtime = ?
            LIMIT 1
            """,
            (device_serial, remote_file.path, remote_file.size, remote_file.mtime),
        ).fetchone()
        return row is not None

    def remember_file(self, device_serial: str, remote_file: RemoteFile, local_path: Path) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO transferred_files (
                device_serial,
                remote_path,
                remote_size,
                remote_mtime,
                local_path
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                device_serial,
                remote_file.path,
                remote_file.size,
                remote_file.mtime,
                str(local_path),
            ),
        )
        self._connection.commit()

    def has_pushed_file(
        self,
        device_serial: str,
        local_path: Path,
        local_size: int,
        local_mtime_ns: int,
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM pushed_files
            WHERE device_serial = ?
              AND local_path = ?
              AND local_size = ?
              AND local_mtime_ns = ?
            LIMIT 1
            """,
            (device_serial, str(local_path), local_size, local_mtime_ns),
        ).fetchone()
        return row is not None

    def remember_pushed_file(
        self,
        device_serial: str,
        local_path: Path,
        local_size: int,
        local_mtime_ns: int,
        remote_path: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO pushed_files (
                device_serial,
                local_path,
                local_size,
                local_mtime_ns,
                remote_path
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_serial, str(local_path), local_size, local_mtime_ns, remote_path),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
