"""The frontend's API client against a stubbed backend (no backend code needed).

Run from the frontend/ folder:  python -m unittest -v
"""

from __future__ import annotations

import json
import unittest

import httpx

from api_client import ApiClient, ApiError, parse_time

PDF = b"%PDF-1.4 pretend statement"


def stub_backend(request: httpx.Request) -> httpx.Response:
    """Answers like the real API for the paths the pages use."""
    path, params = request.url.path, dict(request.url.params)
    if request.method == "GET" and path == "/health":
        return httpx.Response(200, json={"status": "ok", "database": "ok", "database_error": None, "model": "none"})
    if request.method == "POST" and path == "/sync":
        return httpx.Response(202, json={"id": "job1", "state": "running", "started_at": "2026-10-07T09:00:00Z",
                                         "finished_at": None, "result": None, "error": None})
    if request.method == "GET" and path == "/sync/job1":
        return httpx.Response(200, json={"id": "job1", "state": "succeeded", "started_at": "2026-10-07T09:00:00Z",
                                         "finished_at": "2026-10-07T09:00:05Z", "result": {"status": "ok"}, "error": None})
    if request.method == "GET" and path == "/threads":
        return httpx.Response(200, json=[{"id": 7, "subject": "Invoice", "echo": params}])
    if request.method == "GET" and path == "/threads/999":
        return httpx.Response(404, json={"detail": "no thread 999"})
    if request.method == "GET" and path == "/attachments/3":
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    if request.method == "GET" and path == "/schema/ddl":
        return httpx.Response(200, text=f"CREATE TABLE threads -- {params['dialect']}")
    if request.method == "GET" and path == "/tables/emails/rows":
        return httpx.Response(200, json={"name": "emails", "total": 2, "columns": ["id"], "rows": [{"id": 1}], "echo": params})
    if request.method == "POST" and path == "/reclassify":
        return httpx.Response(409, content=json.dumps({"detail": "a sync is running"}))
    return httpx.Response(500, text="unexpected request")


class ApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        http = httpx.Client(base_url="http://backend.test", transport=httpx.MockTransport(stub_backend))
        self.api = ApiClient(base_url="http://backend.test", http=http)

    def test_json_endpoints_and_parameters(self) -> None:
        self.assertEqual(self.api.health()["model"], "none")
        threads = self.api.threads(payment_only=True, search="acme", limit=50)
        self.assertEqual(threads[0]["echo"], {"payment_only": "true", "search": "acme", "limit": "50"})
        rows = self.api.table_rows("emails", limit=10, order_by="received_at", descending=False)
        self.assertEqual(rows["echo"], {"limit": "10", "descending": "false", "order_by": "received_at"})

    def test_sync_job_round_trip(self) -> None:
        job = self.api.start_sync()
        self.assertEqual(job["state"], "running")
        self.assertEqual(self.api.sync_job(job["id"])["state"], "succeeded")

    def test_binary_and_text_responses(self) -> None:
        self.assertEqual(self.api.attachment_bytes(3), PDF)
        self.assertEqual(self.api.ddl("sqlite"), "CREATE TABLE threads -- sqlite")

    def test_http_errors_carry_status_and_detail(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.thread(999)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("no thread 999", str(ctx.exception))

        with self.assertRaises(ApiError) as ctx:
            self.api.reclassify()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_unreachable_backend_explains_how_to_start_it(self) -> None:
        api = ApiClient(base_url="http://127.0.0.1:9")  # nothing listens on the discard port
        with self.assertRaises(ApiError) as ctx:
            api.health()
        self.assertIn("python -m uvicorn api.main:create_app", str(ctx.exception))

    def test_parse_time_accepts_z_and_offsets(self) -> None:
        self.assertEqual(parse_time("2026-10-07T09:00:00Z"), parse_time("2026-10-07T09:00:00+00:00"))
        self.assertIsNone(parse_time(None))


if __name__ == "__main__":
    unittest.main()
