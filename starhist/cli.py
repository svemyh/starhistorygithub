"""Command-line surface. Argument parsing and human output only; the work lives
in auth / github / render, which know nothing about argparse."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from .auth import FileStore, KeychainStore, NullStore, default_store, redact, resolve
from .cache import Cache
from .github import AccessRestricted, Client, GitHubError, Series
from .render import MAX_SERIES, Options, render


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.handler(args)
    except AccessRestricted as exc:
        _fail(str(exc))
    except GitHubError as exc:
        _fail(str(exc))
    except ValueError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        _fail("interrupted")
    return 1


# --- commands ---------------------------------------------------------------

def _cmd_login(args) -> int:
    store = _store(args)
    token = args.token or getpass.getpass("GitHub token (input hidden): ").strip()
    if not token:
        _fail("no token given")
    client = Client(token)
    login = _whoami(client)
    if login is None:
        _fail("GitHub rejected that token.")
    if isinstance(store, NullStore):
        print(f"Verified as {login}. Not stored (--no-store).")
        return 0
    store.set(token)
    print(f"Verified as {login}. Stored in {store.name}.")
    print("Nothing else on this machine holds the token; `starhist auth logout` removes it.")
    return 0


def _cmd_status(args) -> int:
    store = _store(args)
    token, source = resolve(None, store)
    if not token:
        print("Not authenticated.")
        print("Run `starhist auth login`, or install and run `gh auth login`.")
        return 1
    print(f"Token:  {redact(token)}  (from {source})")
    client = Client(token)
    login = _whoami(client)
    if login is None:
        print("Status: REJECTED by GitHub. Re-run `starhist auth login`.")
        return 1
    print(f"User:   {login}")
    if args.repo:
        try:
            count = client.star_count(args.repo)
            client._page(args.repo, 1)
            print(f"Access: can read {args.repo} stargazers ({count} stars)")
        except AccessRestricted:
            print(f"Access: CANNOT read {args.repo} stargazers "
                  f"(you do not administer it; see the 2026 restriction)")
            return 1
    return 0


def _cmd_logout(args) -> int:
    for store in (KeychainStore(), FileStore()):
        if store.available():
            store.clear()
    print("Removed any stored token. Env vars and `gh` are untouched.")
    return 0


def _cmd_chart(args) -> int:
    if len(args.repos) > MAX_SERIES:
        _fail(f"{len(args.repos)} repos given; the palette holds {MAX_SERIES}. "
              f"Beyond that colours stop being distinguishable.")
    series = _collect(args)

    opts = Options(
        width=args.width,
        height=args.height,
        title=args.title or _default_title(args.repos),
        dark=args.dark,
        xkcd=args.style == "xkcd",
        timeline=args.type == "timeline",
        colors=[c if c.startswith("#") else f"#{c}" for c in args.color] or None,
        attribution="" if args.no_attribution else "Made with svemyh/star-history-cli",
    )
    svg = render(series, opts)
    out = Path(args.output or _default_name(args.repos, args.dark))
    out.write_text(svg, encoding="utf-8")

    total = sum(s.total for s in series)
    span = _span(series)
    print(f"Wrote {out}: {len(series)} repo(s), {total} stars{span}")
    if any(s.sampled for s in series):
        print("Note: at least one repo exceeds GitHub's 40,000-stargazer pagination "
              "wall, so its curve is interpolated from evenly-spaced samples and "
              "anchored to the live count.")
    return 0


def _cmd_export(args) -> int:
    series = _collect(args)
    out = Path(args.output or f"star-history.{args.format}")
    if args.format == "json":
        payload = [
            {"repo": s.repo, "total": s.total, "sampled": s.sampled,
             "points": [[t.isoformat(), c] for t, c in s.points]}
            for s in series
        ]
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["repo", "starred_at", "cumulative_stars"])
            for s in series:
                for stamp, count in s.points:
                    writer.writerow([s.repo, stamp.isoformat(), count])
    print(f"Wrote {out}: {sum(len(s.points) for s in series)} rows")
    return 0


def _cmd_cache_clear(args) -> int:
    print(f"Removed {Cache().clear()} cached repo(s).")
    return 0


# --- helpers ----------------------------------------------------------------

def _collect(args) -> list[Series]:
    token, source = resolve(getattr(args, "token", None), _store(args))
    if not token:
        _fail("Not authenticated. Run `starhist auth login` (or `gh auth login`).")
    client = Client(token)
    cache = Cache(ttl=timedelta(0) if args.no_cache else timedelta(hours=6))
    out = []
    for repo in args.repos:
        repo = _normalise(repo)
        cached = None if args.no_cache else cache.get(repo)
        if cached:
            _log(args, f"{repo}: {cached.total} stars (cached)")
            out.append(cached)
            continue
        _log(args, f"{repo}: fetching…")
        series = client.stargazers(repo)
        if not series.points:
            print(f"warning: {repo} has no stars yet, skipping", file=sys.stderr)
            continue
        cache.put(series)
        out.append(series)
    if not out:
        _fail("no repo had any stars to chart")
    return out


def _normalise(repo: str) -> str:
    repo = repo.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    parts = [p for p in repo.split("/") if p]
    if len(parts) != 2:
        raise ValueError(f"expected owner/repo, got {repo!r}")
    return "/".join(parts)


def _whoami(client: Client) -> str | None:
    try:
        return client._get("/user")["login"]  # type: ignore[index]
    except (GitHubError, FileNotFoundError, KeyError, TypeError):
        return None


def _store(args):
    if getattr(args, "no_store", False):
        return NullStore()
    return default_store(prefer_keychain=not getattr(args, "file_store", False))


def _default_title(repos: list[str]) -> str:
    return "Star History"


def _default_name(repos: list[str], dark: bool) -> str:
    stem = "star-history" if len(repos) > 1 else _normalise(repos[0]).split("/")[1]
    return f"{stem}{'-dark' if dark else ''}.svg"


def _span(series: list[Series]) -> str:
    starts = [s.first for s in series if s.first]
    ends = [s.last for s in series if s.last]
    if not starts:
        return ""
    return f", {min(starts):%b %d %Y} to {max(ends):%b %d %Y}"


def _log(args, message: str) -> None:
    if not getattr(args, "quiet", False):
        print(message, file=sys.stderr)


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


# --- parser -----------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="starhist",
        description="GitHub star-history charts from the terminal, including "
                    "multi-repo comparison on one axis.",
        epilog="Since 2026-06-30 GitHub limits stargazer data to repo admins and "
               "collaborators, so you can only chart repos you own or collaborate on.",
    )
    parser.add_argument("--version", action="version", version=f"starhist {__version__}")
    subs = parser.add_subparsers(dest="command")

    auth = subs.add_parser("auth", help="manage the stored GitHub token")
    auth_subs = auth.add_subparsers(dest="auth_command")

    login = auth_subs.add_parser("login", help="verify a token and store it")
    login.add_argument("--token", help="token value (omit to be prompted, hidden)")
    login.add_argument("--no-store", action="store_true", help="verify only, store nothing")
    login.add_argument("--file-store", action="store_true",
                       help="use a 0600 config file instead of the macOS Keychain")
    login.set_defaults(handler=_cmd_login, command="auth")

    status = auth_subs.add_parser("status", help="show which token is in play")
    status.add_argument("--repo", help="also check stargazer access to this repo")
    status.set_defaults(handler=_cmd_status, command="auth")

    logout = auth_subs.add_parser("logout", help="delete the stored token")
    logout.set_defaults(handler=_cmd_logout, command="auth")

    chart = subs.add_parser("chart", help="render an SVG star-history chart")
    _shared(chart)
    chart.add_argument("-o", "--output", help="output path (default: <repo>.svg)")
    chart.add_argument("--title", help='chart title (default: "Star History")')
    chart.add_argument("--style", choices=("xkcd", "clean"), default="xkcd")
    chart.add_argument("--type", choices=("date", "timeline"), default="date",
                       help="date = real dates; timeline = days since each repo's "
                            "first star, which aligns launches for comparison")
    chart.add_argument("--dark", action="store_true")
    chart.add_argument("--width", type=int, default=800)
    chart.add_argument("--height", type=int, default=533)
    chart.add_argument("--color", action="append", default=[],
                       help="override series colour (repeatable, in repo order)")
    chart.add_argument("--no-attribution", action="store_true")
    chart.set_defaults(handler=_cmd_chart)

    export = subs.add_parser("export", help="write the raw curve as CSV or JSON")
    _shared(export)
    export.add_argument("-f", "--format", choices=("csv", "json"), default="csv")
    export.add_argument("-o", "--output")
    export.set_defaults(handler=_cmd_export)

    cache = subs.add_parser("cache", help="manage the local fetch cache")
    cache_subs = cache.add_subparsers(dest="cache_command")
    clear = cache_subs.add_parser("clear", help="delete every cached repo")
    clear.set_defaults(handler=_cmd_cache_clear, command="cache")

    return parser


def _shared(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("repos", nargs="+", metavar="owner/repo",
                     help="one or more repos; URLs are accepted too")
    sub.add_argument("--token", help="use this token instead of the stored one")
    sub.add_argument("--no-cache", action="store_true", help="always refetch")
    sub.add_argument("-q", "--quiet", action="store_true")
