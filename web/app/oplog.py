"""Append-only operation log. Never records secrets or API keys."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("open-controller")

SHANGHAI = ZoneInfo("Asia/Shanghai")

# Fields that must never be persisted if a caller accidentally passes them.
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "api_secret",
    "apisecret",
    "secret",
    "binance_api_key",
    "binance_api_secret",
    "app_token",
    "token",
    "authorization",
    "x-app-token",
    "password",
    "key",
}


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def format_ts(moment: Optional[datetime] = None) -> str:
    stamp = moment or now_shanghai()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI)
    else:
        stamp = stamp.astimezone(SHANGHAI)
    return stamp.isoformat(timespec="seconds")


MAX_BYTES = 256 * 1024
KEEP_LINES = 500


class OpLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": format_ts(), "event": event}
        for key, value in fields.items():
            if key.lower() in _SECRET_KEYS:
                continue
            if value is None:
                continue
            record[key] = value
        line = json.dumps(record, ensure_ascii=False)
        try:
            self._trim_if_large()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            log.warning("写入操作日志失败")
        return record

    def _trim_if_large(self) -> None:
        """Bound file size: when over MAX_BYTES keep the newest KEEP_LINES lines."""
        try:
            if not self.path.exists() or self.path.stat().st_size <= MAX_BYTES:
                return
            with self.path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-KEEP_LINES:]
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                fh.writelines(lines)
            tmp.replace(self.path)
        except Exception:
            log.warning("裁剪操作日志失败")

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return newest-first records, up to ``limit``."""
        if not self.path.exists() or limit <= 0:
            return []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
            if len(out) >= limit:
                break
        return out
