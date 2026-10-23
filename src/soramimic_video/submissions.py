"""Durable, bounded reservations for cancelable browser submissions."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

TTL_SECONDS = 3600


class Submissions:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, submission_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", submission_id):
            raise HTTPException(404, "受付情報が見つかりません")
        return self.root / f"{submission_id}.json"

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            raise HTTPException(410, "受付情報は取り消されたか、期限が切れています") from None

    def create(self, owner: str | None, client_hash: str) -> str:
        with self.lock:
            self.cleanup()
            records = [self._read(path) for path in self.root.glob("*.json")]
            if (len(records) >= 128
                    or sum(r["owner"] == owner for r in records) >= 8
                    or sum(r["client_hash"] == client_hash for r in records) >= 8):
                raise HTTPException(429, "受付が混み合っています。しばらく待ってください")
            submission_id = uuid.uuid4().hex
            record = {"owner": owner, "client_hash": client_hash,
                      "expires_at": time.time() + TTL_SECONDS}
            path = self._path(submission_id)
            temporary = path.with_suffix(".tmp")
            with open(temporary, "x", encoding="utf-8",
                      opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return submission_id

    def require(self, submission_id: str, owner: str | None) -> None:
        with self.lock:
            record = self._read(self._path(submission_id))
            if record["owner"] != owner:
                raise HTTPException(404, "受付情報が見つかりません")
            if record["expires_at"] <= time.time():
                raise HTTPException(410, "受付情報の期限が切れました。もう一度送信してください")

    def remove(self, submission_id: str, owner: str | None) -> None:
        with self.lock:
            path = self._path(submission_id)
            try:
                record = self._read(path)
            except HTTPException as exc:
                if exc.status_code == 410:
                    return  # Missing reservations cannot admit a registered submission.
                raise
            if record["owner"] != owner:
                raise HTTPException(404, "受付情報が見つかりません")
            path.unlink()

    def cleanup(self, now: float | None = None) -> None:
        with self.lock:
            current = time.time() if now is None else now
            for path in self.root.glob("*.json"):
                if self._read(path)["expires_at"] <= current:
                    path.unlink()
            for path in self.root.glob("*.tmp"):
                path.unlink()
