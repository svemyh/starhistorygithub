"""Fetching star timelines from GitHub.

Two facts shape everything here:

1. Since 2026-06-30 the stargazers listing endpoint is limited to repo admins
   and collaborators. For anyone else it 404s, and no token scope fixes it, so
   a 404 is a permissions answer and must be reported as one rather than as a
   missing repo.
2. The endpoint paginates at 400 pages x 100 = 40,000 stargazers. Above that,
   the exact curve is unobtainable and every tool in this category samples
   evenly-spaced pages and anchors the final point to the live count. We do the
   same, and we say so in the output rather than pretending the curve is exact.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 400          # GitHub's hard pagination wall
MAX_EXACT = MAX_PAGES * PER_PAGE

# An HTTP transport: (url, headers) -> (status, body-bytes). Injected so tests
# never touch the network.
Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]


class GitHubError(RuntimeError):
    """Something went wrong that the user needs to read."""


class AccessRestricted(GitHubError):
    """The 2026 stargazers restriction, stated in the terms the user needs."""

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"GitHub returned 404 for {repo}'s stargazers.\n"
            f"Since 2026-06-30 that endpoint is limited to repo admins and "
            f"collaborators, so this means your token does not administer "
            f"{repo} (not that the repo is missing).\n"
            f"No token scope changes this. You can only chart repos you own or "
            f"collaborate on."
        )


@dataclass
class Series:
    """One repo's star curve."""

    repo: str
    points: list[tuple[datetime, int]] = field(default_factory=list)
    total: int = 0
    sampled: bool = False        # True when we hit the 40k wall and interpolated

    @property
    def first(self) -> datetime | None:
        return self.points[0][0] if self.points else None

    @property
    def last(self) -> datetime | None:
        return self.points[-1][0] if self.points else None


def urllib_transport(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise GitHubError(f"network error: {exc.reason}") from exc


class Client:
    """Reads star data. Owns no cache and no rendering, only fetching."""

    def __init__(self, token: str, transport: Transport = urllib_transport) -> None:
        self._token = token
        self._transport = transport

    def _get(self, path: str, accept: str = "application/vnd.github+json") -> object:
        status, body = self._transport(
            f"{API}{path}",
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "starhist",
            },
        )
        if status == 401:
            raise GitHubError("GitHub rejected the token (401). Run `starhist auth login`.")
        if status == 403:
            raise GitHubError(
                "GitHub returned 403. Either rate-limited, or the token lacks "
                "access. Check `starhist auth status`."
            )
        if status == 404:
            raise FileNotFoundError(path)
        if status >= 400:
            raise GitHubError(f"GitHub returned HTTP {status} for {path}")
        return json.loads(body)

    def star_count(self, repo: str) -> int:
        try:
            meta = self._get(f"/repos/{repo}")
        except FileNotFoundError:
            raise GitHubError(f"Repo {repo} not found, or not visible to this token.")
        return int(meta["stargazers_count"])  # type: ignore[index]

    def stargazers(self, repo: str, on_page: Callable[[int, int], None] | None = None) -> Series:
        """Build the cumulative curve for one repo."""
        total = self.star_count(repo)
        series = Series(repo=repo, total=total)
        if total == 0:
            return series

        pages = min((total + PER_PAGE - 1) // PER_PAGE, MAX_PAGES)
        if total > MAX_EXACT:
            # Above the wall: sample evenly across the 400 reachable pages.
            series.sampled = True
            wanted = _even_pages(pages, count=min(pages, 60))
        else:
            wanted = list(range(1, pages + 1))

        stamps: list[datetime] = []
        for index, page in enumerate(wanted, start=1):
            rows = self._page(repo, page)
            if not rows:
                break
            stamps.extend(_parse_times(rows))
            if on_page:
                on_page(index, len(wanted))

        if not stamps:
            return series
        stamps.sort()
        series.points = _cumulative(stamps, total=total, sampled=series.sampled)
        return series

    def _page(self, repo: str, page: int) -> list[dict]:
        try:
            rows = self._get(
                f"/repos/{repo}/stargazers?per_page={PER_PAGE}&page={page}",
                accept="application/vnd.github.star+json",
            )
        except FileNotFoundError:
            raise AccessRestricted(repo) from None
        return rows if isinstance(rows, list) else []


def _parse_times(rows: Iterable[dict]) -> list[datetime]:
    out = []
    for row in rows:
        raw = row.get("starred_at") if isinstance(row, dict) else None
        if raw:
            out.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return out


def _even_pages(pages: int, count: int) -> list[int]:
    """Evenly spaced page numbers, always including the first and last."""
    if pages <= count:
        return list(range(1, pages + 1))
    step = (pages - 1) / (count - 1)
    return sorted({1 + round(i * step) for i in range(count)})


def _cumulative(
    stamps: list[datetime], total: int, sampled: bool
) -> list[tuple[datetime, int]]:
    """Turn sorted star timestamps into a cumulative curve.

    When sampled, the y values are scaled so the curve ends at the live count:
    we saw a fixed fraction of stars, spread over the true time range, so the
    shape is right even though individual points are interpolated.
    """
    scale = (total / len(stamps)) if sampled and stamps else 1.0
    points = [(stamp, round((i + 1) * scale)) for i, stamp in enumerate(stamps)]
    if points:
        # Anchor the end to the number GitHub reports right now.
        points[-1] = (max(points[-1][0], datetime.now(timezone.utc)), total)
    return points
