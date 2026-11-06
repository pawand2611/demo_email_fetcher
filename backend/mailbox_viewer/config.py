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
    # Trained models, local folders. Missing folder = that model is skipped.
    attachment_model_dir: str | None = "models/layoutlmv3_invoice"
    body_model_dir: str | None = "models/laya"
    # Attachment model (LayoutLMv3): an invoice counts from this score.
    model_min_confidence: float = 0.5
    # Body model (Laya): a statement / invoice / receipt counts as payment only from this score.
    body_min_confidence: float = 0.7
    # Attachment model: above this score the attachment decides on its own and Laya is not run.
    attachment_decides_confidence: float = 0.8
    # Name of the business purpose this deployment serves.
    profile_name: str = "statement_recon"
    # Tickets: a reply that Laya scores at least this sure is a payment confirmation closes the ticket.
    ticket_close_confidence: float = 0.8
    # Intent classification of payment emails (intent.py). LAYA_DIR unset = intent off.
    laya_dir: str | None = None
    laya_checkpoint: str = "typed-decisions"
    tier1_min_confidence: float = 0.4
    tier2_enabled: bool = True
    tier2_url: str = "http://127.0.0.1:8080"
    tier2_timeout_s: float = 900.0  # CPU; about 60 on a GPU
    tier2_max_tokens: int = 1536
    tier2_temperature: float = 0.6
    email_body_max_chars: int = 3000
    # Tier 2 inside the classification flow (GPU only); otherwise a background job.
    tier2_inline: bool = False
    # Emails one tier-2 background job resolves before it ends (each takes ~100 s on CPU).
    tier2_max_per_job: int = 10

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
    "MODEL_MIN_CONFIDENCE": "0.5",
    "BODY_MIN_CONFIDENCE": "0.7",
    "ATTACHMENT_DECIDES_CONFIDENCE": "0.8",
    "ATTACHMENT_MODEL_DIR": "models/layoutlmv3_invoice",
    "BODY_MODEL_DIR": "models/laya",
    "PROFILE_NAME": "statement_recon",
    "TICKET_CLOSE_CONFIDENCE": "0.8",
    "LAYA_CHECKPOINT": "typed-decisions",
    "TIER1_MIN_CONFIDENCE": "0.4",
    "TIER2_ENABLED": "true",
    "TIER2_URL": "http://127.0.0.1:8080",
    "TIER2_TIMEOUT_S": "900",
    "TIER2_MAX_TOKENS": "1536",
    "TIER2_TEMPERATURE": "0.6",
    "EMAIL_BODY_MAX_CHARS": "3000",
    "TIER2_INLINE": "false",
    "TIER2_MAX_PER_JOB": "10",
}

# Email content may only go to a model on this machine.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
        attachment_model_dir=_get("ATTACHMENT_MODEL_DIR") or None,
        body_model_dir=_get("BODY_MODEL_DIR") or None,
        model_min_confidence=_get_fraction("MODEL_MIN_CONFIDENCE"),
        body_min_confidence=_get_fraction("BODY_MIN_CONFIDENCE"),
        attachment_decides_confidence=_get_fraction("ATTACHMENT_DECIDES_CONFIDENCE"),
        profile_name=_get("PROFILE_NAME") or "statement_recon",
        ticket_close_confidence=_get_fraction("TICKET_CLOSE_CONFIDENCE"),
        laya_dir=_get("LAYA_DIR") or None,
        laya_checkpoint=_get("LAYA_CHECKPOINT") or "typed-decisions",
        tier1_min_confidence=_get_fraction("TIER1_MIN_CONFIDENCE"),
        tier2_enabled=_get_bool("TIER2_ENABLED"),
        tier2_url=_get_local_url("TIER2_URL"),
        tier2_timeout_s=_get_positive_float("TIER2_TIMEOUT_S"),
        tier2_max_tokens=_get_int("TIER2_MAX_TOKENS"),
        tier2_temperature=_get_temperature("TIER2_TEMPERATURE"),
        email_body_max_chars=_get_int("EMAIL_BODY_MAX_CHARS"),
        tier2_inline=_get_bool("TIER2_INLINE"),
        tier2_max_per_job=_get_int("TIER2_MAX_PER_JOB"),
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


def _get_fraction(key: str) -> float:
    raw = _get(key)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number between 0 and 1, got {raw!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"{key} must be between 0 and 1, got {value}")
    return value


def _get_positive_float(key: str) -> float:
    raw = _get(key)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be greater than 0, got {value}")
    return value


def _get_temperature(key: str) -> float:
    raw = _get(key)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number between 0 and 2, got {raw!r}") from exc
    if not 0.0 <= value <= 2.0:
        raise ConfigError(f"{key} must be between 0 and 2, got {value}")
    return value


def _get_local_url(key: str) -> str:
    """A URL on this machine only: the tier-2 model reads email content."""
    from urllib.parse import urlparse

    raw = _get(key).rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _LOCAL_HOSTS:
        raise ConfigError(f"{key} must be an http(s) URL on localhost (127.0.0.1, localhost or ::1), got {raw!r}")
    return raw


def _get_int(key: str) -> int:
    raw = _get(key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {value}")
    return value
