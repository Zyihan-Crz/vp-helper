"""Import only judge cookies from the local Firefox profile and verify its user."""
from __future__ import annotations

import configparser
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

_MAX_SESSION_SIZE = 16 * 1024 * 1024
_cache: dict = {}
_lock = threading.Lock()


@dataclass
class BrowserCookies:
    profile: Path
    cookie: str = field(repr=False)


def firefox_profiles(account: dict) -> list[Path]:
    explicit = account.get("firefox_profile")
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    if os.name != "posix":
        return []
    home = Path.home()
    if os.geteuid() == 0 and os.environ.get("SUDO_USER") not in (None, "", "root"):
        import pwd
        try:
            home = Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            pass
    profiles = []
    for base in (home / ".mozilla/firefox", home / "snap/firefox/common/.mozilla/firefox"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(base / "profiles.ini")
            preferred = [parser[s]["Default"] for s in parser.sections()
                         if s.startswith("Install") and parser[s].get("Default")]
            entries = [s for s in parser.sections() if s.startswith("Profile")]
            entries.sort(key=lambda s: (parser[s].get("Path") not in preferred,
                                       parser[s].get("Default") != "1"))
            for section in entries:
                name = parser[section].get("Path")
                if not name:
                    continue
                path = (base / name if parser[section].get("IsRelative", "1") == "1" else Path(name)).resolve()
                if path.is_dir() and path not in profiles:
                    profiles.append(path)
        except (OSError, configparser.Error):
            continue
    return profiles


def _decode_session(data: bytes) -> dict:
    """Firefox's mozLz40 header precedes a size and one LZ4 block."""
    if data[:8] != b"mozLz40\0" or len(data) < 12:
        raise ValueError("Invalid Firefox session header")
    expected = int.from_bytes(data[8:12], "little")
    if not 0 < expected <= _MAX_SESSION_SIZE:
        raise ValueError("Firefox session size is outside the supported range")
    block, position, result = memoryview(data)[12:], 0, bytearray()

    def length(value):
        nonlocal position
        if value == 15:
            while True:
                if position >= len(block):
                    raise ValueError("Truncated Firefox session")
                extra = block[position]
                position += 1
                value += extra
                if extra != 255:
                    break
        return value

    while position < len(block):
        token = block[position]
        position += 1
        literals = length(token >> 4)
        if position + literals > len(block) or len(result) + literals > expected:
            raise ValueError("Invalid Firefox session literals")
        result.extend(block[position:position + literals])
        position += literals
        if position == len(block):
            break
        if position + 2 > len(block):
            raise ValueError("Truncated Firefox session offset")
        distance = int.from_bytes(block[position:position + 2], "little")
        position += 2
        count = length(token & 15) + 4
        if not 0 < distance <= len(result) or len(result) + count > expected:
            raise ValueError("Invalid Firefox session match")
        for _ in range(count):
            result.append(result[-distance])
    if len(result) != expected:
        raise ValueError("Firefox session size mismatch")
    value = json.loads(result)
    if not isinstance(value, dict):
        raise ValueError("Invalid Firefox session object")
    return value


def _ordinary(attributes) -> bool:
    return not any(attributes.values()) if isinstance(attributes, dict) else attributes in (None, "")


def _session_cookies(profile: Path, host: str) -> dict:
    files = [profile / "sessionstore.jsonlz4",
             profile / "sessionstore-backups/recovery.jsonlz4",
             profile / "sessionstore-backups/recovery.baklz4",
             profile / "sessionstore-backups/previous.jsonlz4"]
    try:
        existing = [p for p in files if p.is_file()]
        if not existing:
            return {}
        latest = max(existing, key=lambda p: p.stat().st_mtime_ns)
        if latest.stat().st_size > _MAX_SESSION_SIZE:
            return {}
        state = _decode_session(latest.read_bytes())
        cookies = list(state.get("cookies", []))
        for window in state.get("windows", []):
            if not window.get("isPrivate"):
                cookies.extend(window.get("cookies", []))
        return {c["name"]: c["value"] for c in cookies
                if c.get("host", "").lstrip(".") == host and c.get("path", "/") == "/"
                and _ordinary(c.get("originAttributes")) and c.get("expiry", float("inf")) > time.time()
                and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def _header(values: dict) -> str:
    jar = SimpleCookie()
    for name, value in values.items():
        if (re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)):
            jar[name] = value
    return "; ".join(m.OutputString() for m in jar.values())


def browser_cookies(platform: str, account: dict) -> list[BrowserCookies]:
    host = "qoj.ac" if platform == "qoj" else "codeforces.com"
    candidates = []
    for profile in firefox_profiles(account):
        persistent = {}
        database = profile / "cookies.sqlite"
        try:
            if database.is_file():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as conn:
                    persistent = dict(conn.execute(
                        "SELECT name, value FROM moz_cookies WHERE host IN (?, ?) AND path='/' "
                        "AND expiry > ? AND originAttributes='' ORDER BY lastAccessed",
                        (host, "." + host, int(time.time()))))
        except (OSError, sqlite3.Error):
            pass
        combined = {**persistent, **_session_cookies(profile, host)}
        seen = set()
        for values in (combined, persistent):
            header = _header(values)
            if header and header not in seen:
                candidates.append(BrowserCookies(profile, header))
                seen.add(header)
    return candidates


def _verify(platform: str, candidate: BrowserCookies, expected: str) -> dict | None:
    from .http import WebSession
    host = "qoj.ac" if platform == "qoj" else "codeforces.com"
    origin = "https://" + host
    web = WebSession(origin, {"cookie": candidate.cookie}, auto_refresh=False)
    try:
        response = web.session.get(origin + "/", params={"locale": "en"}, timeout=(5, 15))
        web.check(response)
        if urlsplit(response.url).hostname != host:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        selector = "ul.nav-pills, .navbar, nav, header, .nav-item.dropdown .dropdown-menu" if platform == "qoj" else "#header"
        prefix = "/user/profile/" if platform == "qoj" else "/profile/"
        handles = {urlsplit(a.get("href", "")).path[len(prefix):].rstrip("/")
                   for header in soup.select(selector) for a in header.select("a[href]")
                   if urlsplit(a["href"]).path.startswith(prefix)}
        if expected:
            handles = {h for h in handles if h == expected or platform != "qoj" and h.lower() == expected.lower()}
        if len(handles) != 1:
            return None
        values = {c.name: c.value for c in web.session.cookies.jar
                  if c.domain.lstrip(".") == host and c.path == "/"}
        return {"cookie": _header(values), "handle": expected or handles.pop(),
                "cookie_source": "firefox", "firefox_profile": str(candidate.profile)}
    except Exception:
        # Do not replace a configured session on network failure or another login.
        return None
    finally:
        web.session.close()


def refresh_browser_account(platform: str, account: dict, *, force: bool = False) -> bool:
    if account.get("cookie_source") == "manual":
        return False
    for candidate in browser_cookies(platform, account):
        key = ("qoj" if platform == "qoj" else "cf", str(candidate.profile), account.get("handle", ""),
               hashlib.sha256(candidate.cookie.encode()).digest())
        with _lock:
            cached = _cache.get(key)
            if not force and cached and time.monotonic() - cached[0] < 30:
                verified = cached[1]
            else:
                verified = _verify(platform, candidate, account.get("handle", ""))
                _cache[key] = (time.monotonic(), verified)
        if verified:
            account.update(verified)
            return True
    return False


def browser_fields(account: dict) -> dict:
    return {k: account[k] for k in ("cookie_source", "firefox_profile") if account.get(k)}


def refresh_submission_login(path: Path, config: dict, platform: str) -> None:
    from .storage import write_json
    before = dict(config)
    if refresh_browser_account(platform, config) and config != before:
        write_json(path, config)
