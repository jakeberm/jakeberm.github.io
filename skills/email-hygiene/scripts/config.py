"""Shared paths, profile loading, and state for the email-hygiene skill.

All user data lives under EMAIL_HYGIENE_HOME (default ~/.email-hygiene), never in the repo.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = SKILL_ROOT / "config" / "profile.template.json"

APP_DIR = Path(os.environ.get("EMAIL_HYGIENE_HOME") or (Path.home() / ".email-hygiene"))
PROFILE_PATH = APP_DIR / "profile.json"
TOKEN_CACHE_PATH = APP_DIR / "token_cache.bin"
STATE_PATH = APP_DIR / "state.json"
ANALYSIS_PATH = APP_DIR / "analysis.json"
DEEP_SCAN_PATH = APP_DIR / "deep_scan.json"
DECISIONS_PATH = APP_DIR / "decisions.json"
REPORT_DIR = APP_DIR / "reports"
LOG_DIR = APP_DIR / "logs"
AUDIT_PATH = LOG_DIR / "actions.jsonl"

# "Microsoft Graph Command Line Tools" — a Microsoft-owned public client. Used only as a
# fallback; register your own app if this is rejected for consumer sign-in.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/consumers"

# offline_access/openid/profile are added by MSAL automatically and must not be listed.
SCOPES = [
    "Mail.ReadWrite",
    "Mail.Send",
    "MailboxSettings.ReadWrite",
    "User.Read",
]


class ProfileError(RuntimeError):
    """Raised when the profile is missing or malformed."""


def init_console() -> None:
    """Make stdout/stderr survive emoji and non-cp1252 subject lines.

    Windows consoles default to cp1252, so printing a subject containing an
    emoji raises UnicodeEncodeError and kills a long scan mid-run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


init_console()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def domain_matches(domain: str, blocklist) -> bool:
    """True if `domain` is in the blocklist, or is a subdomain of an entry.

    Spam operators rent a domain and then rotate a fresh subdomain per send, so
    an exact match ages out almost immediately. The server-side rules use
    Graph's `senderContains`, which is substring-based and already behaves this
    way; matching on suffix here keeps the local sweep from disagreeing with the
    rules it just wrote.
    """
    domain = (domain or "").lower().lstrip("@")
    if not domain:
        return False
    for entry in blocklist:
        entry = (entry or "").lower().lstrip("@")
        if entry and (domain == entry or domain.endswith("." + entry)):
            return True
    return False


def ensure_dirs() -> None:
    for d in (APP_DIR, REPORT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_template() -> dict:
    with TEMPLATE_PATH.open(encoding="utf-8") as fh:
        template = json.load(fh)
    template.pop("$comment", None)
    return template


def load_profile() -> dict:
    """Load the user profile, layered over the shipped template defaults."""
    if not PROFILE_PATH.exists():
        raise ProfileError(
            f"No profile at {PROFILE_PATH}.\n"
            f"Run:  python scripts/setup.py --email you@example.com"
        )
    with PROFILE_PATH.open(encoding="utf-8") as fh:
        user = json.load(fh)
    user.pop("$comment", None)
    profile = _deep_merge(load_template(), user)

    if not profile.get("account", {}).get("email"):
        raise ProfileError(f"account.email is not set in {PROFILE_PATH}")
    if not profile["account"].get("client_id"):
        profile["account"]["client_id"] = DEFAULT_CLIENT_ID
    if not profile["account"].get("authority"):
        profile["account"]["authority"] = DEFAULT_AUTHORITY
    return profile


def save_profile(profile: dict) -> None:
    ensure_dirs()
    tmp = PROFILE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2)
    tmp.replace(PROFILE_PATH)
    try:
        os.chmod(PROFILE_PATH, 0o600)
    except OSError:
        pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload) -> None:
    ensure_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_state() -> dict:
    return load_json(STATE_PATH, {})


def save_state(state: dict) -> None:
    save_json(STATE_PATH, state)


def audit(event: str, **fields) -> None:
    """Append one line to the immutable-ish action log."""
    ensure_dirs()
    record = {"ts": iso(utcnow()), "event": event, **fields}
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
