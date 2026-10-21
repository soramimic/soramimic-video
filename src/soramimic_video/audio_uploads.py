"""Bounded, expiring audio transfers; offsets are derived from durable file sizes."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

CHUNK_BYTES = 512 * 1024
UPLOAD_TTL_SECONDS = 3600


class AudioUploads:
    def __init__(self, root: Path, maximum: int) -> None:
        self.root = root
        self.maximum = maximum
        self.lock = threading.RLock()
        self.cleanup_failed = False
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cleanup()

    def _directory(self, upload_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise HTTPException(404, "送信データが見つかりません")
        return self.root / upload_id

    def _record(self, upload_id: str, owner: str | None) -> dict[str, Any]:
        directory = self._directory(upload_id)
        try:
            record = json.loads((directory / "upload.json").read_text())
        except (OSError, ValueError):
            raise HTTPException(404, "送信データが見つかりません") from None
        if record["owner"] != owner:
            raise HTTPException(404, "送信データが見つかりません")
        if time.time() >= record["expires_at"]:
            shutil.rmtree(directory)
            raise HTTPException(410, "送信データの期限が切れました。もう一度送信してください")
        return record

    def info(self, upload_id: str, owner: str | None) -> dict[str, Any]:
        with self.lock:
            record = self._record(upload_id, owner)
            return {"id": upload_id, "size": record["size"],
                    "offset": (self._directory(upload_id) / "data").stat().st_size,
                    "chunk_bytes": CHUNK_BYTES}

    def create(
        self, upload_id: str, owner: str | None, client_hash: str,
        filename: str, size: int,
    ) -> dict[str, Any]:
        with self.lock:
            self.cleanup()
            directory = self._directory(upload_id)
            if directory.exists():
                record = self._record(upload_id, owner)
                if record["filename"] != filename or record["size"] != size:
                    raise HTTPException(409, "送信ファイルが一致しません")
                return self.info(upload_id, owner)
            if not 0 < size <= self.maximum:
                raise HTTPException(413, "音源の容量が上限を超えています")
            records = [json.loads(p.read_text()) for p in self.root.glob("*/upload.json")]
            if (len(records) >= 8
                or sum(r["size"] for r in records) + size > self.maximum * 4
                or sum(r["owner"] == owner for r in records) >= 2
                or sum(r["client_hash"] == client_hash for r in records) >= 2):
                raise HTTPException(429, "送信が混み合っています。しばらく待ってください")
            directory.mkdir(mode=0o700)
            try:
                (directory / "data").touch(mode=0o600)
                record = {"owner": owner, "client_hash": client_hash, "filename": filename,
                          "size": size, "expires_at": time.time() + UPLOAD_TTL_SECONDS}
                temporary = directory / "upload.tmp"
                temporary.write_text(json.dumps(record), encoding="utf-8")
                os.replace(temporary, directory / "upload.json")
            except BaseException:
                shutil.rmtree(directory)
                raise
            return self.info(upload_id, owner)

    def append(
        self, upload_id: str, owner: str | None, offset: int, data: bytes,
    ) -> dict[str, Any]:
        with self.lock:
            record = self._record(upload_id, owner)
            path = self._directory(upload_id) / "data"
            current = path.stat().st_size
            if not data or len(data) > CHUNK_BYTES or offset < 0:
                raise HTTPException(413, "送信部分の容量が不正です")
            if offset + len(data) > record["size"]:
                raise HTTPException(413, "音源の容量が申告されたサイズを超えています")
            with path.open("r+b") as stream:
                if offset < current and offset + len(data) <= current:
                    stream.seek(offset)
                    if stream.read(len(data)) == data:
                        return self.info(upload_id, owner)  # Lost acknowledgement.
                if offset != current:
                    raise HTTPException(409, self.info(upload_id, owner))
                stream.seek(current)
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError:
                    stream.truncate(current)
                    raise
            return self.info(upload_id, owner)

    def completed(self, upload_id: str, owner: str | None) -> tuple[Path, str]:
        with self.lock:
            record = self._record(upload_id, owner)
            path = self._directory(upload_id) / "data"
            if path.stat().st_size != record["size"]:
                raise HTTPException(409, "音源の送信が完了していません")
            return path, record["filename"]

    def remove(self, upload_id: str, owner: str | None) -> None:
        with self.lock:
            record = self._record(upload_id, owner)
            directory = self._directory(upload_id)
            record["expires_at"] = 0
            temporary = directory / "upload.tmp"
            temporary.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temporary, directory / "upload.json")
            try:
                shutil.rmtree(directory)
            except OSError:
                self.cleanup_failed = True
                raise

    def cleanup(
        self, now: float | None = None, consumed: dict[str, str | None] | None = None,
    ) -> None:
        with self.lock:
            self.cleanup_failed = True
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                try:
                    record = json.loads((directory / "upload.json").read_text())
                    expired = (time.time() if now is None else now) >= record["expires_at"]
                    if (consumed is not None and directory.name in consumed
                            and consumed[directory.name] == record["owner"]):
                        expired = True
                except (OSError, ValueError, KeyError, TypeError):
                    expired = True
                if expired:
                    shutil.rmtree(directory)
            self.cleanup_failed = False
