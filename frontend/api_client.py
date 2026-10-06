"""Thin HTTP client for the backend API, used by the Streamlit pages.

The frontend never imports the database, mailbox or model code; everything
goes through these calls. ``API_URL`` (default http://127.0.0.1:8000) says
where the backend runs.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv

DEFAULT_API_URL = "http://127.0.0.1:8000"
START_HINT = "uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000"


class ApiError(RuntimeError):
    """The backend is unreachable or answered with an error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def api_url_from_env() -> str:
    load_dotenv(override=False)
    return (os.getenv("API_URL") or DEFAULT_API_URL).rstrip("/")


class ApiClient:
    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or api_url_from_env()).rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- service --------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def profiles(self) -> list[dict[str, Any]]:
        return self._json("GET", "/profiles")

    def stats(self) -> dict[str, Any]:
        return self._json("GET", "/stats")

    # -- sync -----------------------------------------------------------------------

    def start_sync(self) -> dict[str, Any]:
        return self._json("POST", "/sync")

    def sync_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/sync/{job_id}")

    def latest_sync(self) -> dict[str, Any] | None:
        return self._json("GET", "/sync/latest")

    def reclassify(self) -> dict[str, Any]:
        return self._json("POST", "/reclassify")

    # -- mail -----------------------------------------------------------------------

    def threads(self, *, payment_only: bool = False, search: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"payment_only": payment_only, "limit": limit}
        if search:
            params["search"] = search
        return self._json("GET", "/threads", params=params)

    def thread(self, thread_id: int) -> dict[str, Any]:
        return self._json("GET", f"/threads/{thread_id}")

    def attachment_bytes(self, attachment_id: int) -> bytes:
        return self._request("GET", f"/attachments/{attachment_id}").content

    # -- data model -----------------------------------------------------------------

    def schema(self, dialect: str | None = None) -> list[dict[str, Any]]:
        return self._json("GET", "/schema", params={"dialect": dialect} if dialect else None)

    def ddl(self, dialect: str) -> str:
        return self._request("GET", "/schema/ddl", params={"dialect": dialect}).text

    def tables(self) -> list[dict[str, Any]]:
        return self._json("GET", "/tables")

    def table_rows(self, name: str, *, limit: int = 100, order_by: str | None = None, descending: bool = True) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "descending": descending}
        if order_by:
            params["order_by"] = order_by
        return self._json("GET", f"/tables/{name}/rows", params=params)

    # -- transport ------------------------------------------------------------------

    def _json(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params=params).json()

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._http.request(method, path, params=params)
        except httpx.TransportError as exc:
            raise ApiError(f"Backend not reachable at {self.base_url} ({type(exc).__name__}). Start it with: {START_HINT}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(f"{method} {path} failed ({response.status_code}): {detail}", response.status_code)
        return response


def parse_time(value: str | None) -> datetime | None:
    """API timestamps are ISO 8601 with an offset; return local naive time for display."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone().replace(tzinfo=None)
