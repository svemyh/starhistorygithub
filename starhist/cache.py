"""On-disk cache of fetched star curves.

Fetching 70 stars is instant; fetching 40,000 is not, and the data is
append-only in practice. The cache is keyed on repo and stores the raw curve, so
a repeat run of the same chart costs nothing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .github import Series


class Cache:
    def __init__(self, root: Path | None = None, ttl: timedelta = timedelta(hours=6)) -> None:
        self.root = root or _cache_dir()
        self.ttl = ttl

    def _path(self, repo: str) -> Path:
        return self.root / f"{repo.replace('/', '__')}.json"

    def get(self, repo: str) -> Series | None:
        path = self._path(repo)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        fetched = datetime.fromisoformat(raw["fetched_at"])
        if datetime.now(timezone.utc) - fetched > self.ttl:
            return None
        return Series(
            repo=raw["repo"],
            total=raw["total"],
            sampled=raw.get("sampled", False),
            points=[(datetime.fromisoformat(t), c) for t, c in raw["points"]],
        )

    def put(self, series: Series) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "repo": series.repo,
            "total": series.total,
            "sampled": series.sampled,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "points": [[t.isoformat(), c] for t, c in series.points],
        }
        tmp = self._path(series.repo).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path(series.repo))  # atomic, so a kill never corrupts

    def clear(self) -> int:
        if not self.root.exists():
            return 0
        files = list(self.root.glob("*.json"))
        for path in files:
            path.unlink(missing_ok=True)
        return len(files)


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) / "starhist" if base else Path.home() / ".cache" / "starhist"
