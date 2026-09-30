"""Application settings, loaded from the environment or a .env file.

Nothing in the application is hardcoded: every credential and tunable comes
through :func:`load_settings`. Defaults exist only for non-secret values
(port, folder, database URL, limits) and every one of them can be overridden.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or has an invalid value."""


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    imap_timeout: int
    database_url: str
    initial_fetch_limit: int
    attachment_dir: str = "data/attachments"
    db_schema: str | None = None  # PostgreSQL schema for the tables; None = server default (public)
    # True: keep only payment documents and mail in payment threads; other mail
    # leaves just a decision_log line. False: keep every message.
    store_only_payment: bool = False
    # Every folder to sync, each with its own bookmark. Defaults to (imap_folder,).
    imap_folders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.imap_folders:
            object.__setattr__(self, "imap_folders", (self.imap_folder,))


_REQUIRED = ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")

_DEFAULTS = {
    "IMAP_PORT": "993",
    "IMAP_FOLDER": "INBOX",
    "IMAP_TIMEOUT": "30",
    "DATABASE_URL": "sqlite:///data/mailbox_cache.db",
    "INITIAL_FETCH_LIMIT": "200",
    "ATTACHMENT_DIR": "data/attachments",
    "STORE_ONLY_PAYMENT": "false",
}


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """Read settings from the process environment, filling gaps from ``.env``.

    Real environment variables always win over the ``.env`` file, which is the
    conventional behaviour for twelve-factor style configuration.
    """
    load_dotenv(env_file, override=False)

    missing = [key for key in _REQUIRED if not os.getenv(key)]
    if missing:
        raise ConfigError(
            "Missing required settings: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    # IMAP_FOLDERS is a comma-separated list, e.g. "INBOX,[Gmail]/Sent Mail".
    # When absent, only IMAP_FOLDER is synced.
    folders = tuple(f.strip() for f in _get("IMAP_FOLDERS").split(",") if f.strip()) or (_get("IMAP_FOLDER"),)

    db_schema = _get("DB_SCHEMA") or None
    if db_schema and not _IDENTIFIER_RE.match(db_schema):
        raise ConfigError(f"DB_SCHEMA must be a plain identifier (letters, digits, underscore), got {db_schema!r}")

    return Settings(
        imap_host=_get("IMAP_HOST"),
        imap_port=_get_int("IMAP_PORT"),
        imap_user=_get("IMAP_USER"),
        imap_password=_get("IMAP_PASSWORD"),
        imap_folder=folders[0],
        imap_timeout=_get_int("IMAP_TIMEOUT"),
        database_url=_get("DATABASE_URL"),
        initial_fetch_limit=_get_int("INITIAL_FETCH_LIMIT"),
        attachment_dir=_get("ATTACHMENT_DIR") or "data/attachments",
        db_schema=db_schema,
        store_only_payment=_get_bool("STORE_ONLY_PAYMENT"),
        imap_folders=folders,
    )


def _get(key: str) -> str:
    return os.getenv(key, _DEFAULTS.get(key, "")).strip()


def _get_bool(key: str) -> bool:
    raw = _get(key).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")


def _get_int(key: str) -> int:
    raw = _get(key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {value}")
    return value
