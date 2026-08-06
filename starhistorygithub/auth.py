"""Token acquisition and storage.

A GitHub token is the only secret this tool touches, so the rules are narrow and
explicit: a store either holds a token or it does not, and `resolve` walks a
fixed precedence chain without ever writing anything back. Callers inject the
store, so tests never go near the real Keychain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

SERVICE = "starhistorygithub"
ACCOUNT = "github-token"

# Precedence, highest first. Explicit beats ambient beats stored beats borrowed.
SOURCES = ("--token", "STARHISTORYGITHUB_TOKEN", "GITHUB_TOKEN", "stored", "gh CLI")


class TokenStore(Protocol):
    """Somewhere a token can be kept between runs."""

    name: str

    def get(self) -> str | None: ...
    def set(self, token: str) -> None: ...
    def clear(self) -> None: ...
    def available(self) -> bool: ...


class KeychainStore:
    """macOS Keychain, via the `security` binary. No third-party bindings."""

    name = "macOS Keychain"

    def available(self) -> bool:
        return os.uname().sysname == "Darwin" and _which("security")

    def get(self) -> str | None:
        out = _run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]
        )
        return out.strip() if out else None

    def set(self, token: str) -> None:
        # -U updates in place when the entry already exists.
        _run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT,
             "-w", token, "-D", "starhistorygithub github token"],
            check=True,
        )

    def clear(self) -> None:
        _run(["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT])


class FileStore:
    """0600 file under the user's config dir. The portable fallback."""

    name = "config file"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _config_dir() / "token"

    def available(self) -> bool:
        return True

    def get(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def set(self, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the outset; never widen an existing file.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class NullStore:
    """Explicitly stores nothing. Used by --no-store and by tests."""

    name = "none"

    def available(self) -> bool:
        return True

    def get(self) -> str | None:
        return None

    def set(self, token: str) -> None:
        return None

    def clear(self) -> None:
        return None


def default_store(prefer_keychain: bool = True) -> TokenStore:
    keychain = KeychainStore()
    if prefer_keychain and keychain.available():
        return keychain
    return FileStore()


def gh_cli_token() -> str | None:
    """Borrow the `gh` CLI's token, so an already-authenticated user does nothing."""
    if not _which("gh"):
        return None
    out = _run(["gh", "auth", "token"])
    return out.strip() if out else None


def resolve(
    explicit: str | None,
    store: TokenStore,
    env: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return (token, human-readable source). Never writes."""
    env = os.environ if env is None else env
    if explicit:
        return explicit, "--token"
    for var in ("STARHISTORYGITHUB_TOKEN", "GITHUB_TOKEN"):
        value = env.get(var)
        if value:
            return value, f"${var}"
    stored = store.get()
    if stored:
        return stored, store.name
    borrowed = gh_cli_token()
    if borrowed:
        return borrowed, "gh CLI"
    return None, "nothing"


def redact(token: str) -> str:
    """Never print a whole token, not even in --verbose."""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}…{token[-4:]}"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "starhistorygithub" if base else Path.home() / ".config" / "starhistorygithub"


def _which(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run(args: list[str], check: bool = False) -> str | None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        if check:
            raise RuntimeError(proc.stderr.strip() or f"{args[0]} failed")
        return None
    return proc.stdout
