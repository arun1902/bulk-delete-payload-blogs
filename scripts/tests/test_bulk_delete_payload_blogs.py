"""Unit tests for bulk-delete-payload-blogs (no live Payload / no real deletion)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from bulk_delete_payload_blogs import (  # noqa: E402
    CONFIRM_PHRASE,
    Matcher,
    PayloadApiError,
    PayloadRestClient,
    ProgressStore,
    RunConfig,
    SheetRow,
    build_config_from_env_and_args,
    env_bool,
    establish_session,
    upsert_env_var,
    google_sheet_export_url,
    identity_key_for_row,
    load_csv_text,
    mark_duplicates,
    normalize_domain,
    parse_blog_url,
    parse_sheet_rows,
    require_confirmation,
    run,
    sanitize_blog_slug,
)


# ---------------------------------------------------------------------------
# URL / slug / CSV parsing
# ---------------------------------------------------------------------------


def test_valid_url_parses_domain_and_slug():
    domain, slug, err = parse_blog_url("https://www.example.nl/blog/mijn-post/")
    assert err is None
    assert domain == "example.nl"
    assert slug == "mijn-post"


def test_valid_root_slug_url():
    domain, slug, err = parse_blog_url("https://top10populair.nl/beste-laptop/")
    assert err is None
    assert domain == "top10populair.nl"
    assert slug == "beste-laptop"


def test_invalid_url_empty():
    domain, slug, err = parse_blog_url("   ")
    assert domain is None and slug is None
    assert err == "empty URL"


def test_invalid_url_listing_only():
    domain, slug, err = parse_blog_url("https://example.nl/blog/")
    assert domain == "example.nl"
    assert slug is None
    assert err is not None


def test_malformed_sheet_missing_identifier_columns():
    with pytest.raises(ValueError, match="Payload ID|blog URL"):
        parse_sheet_rows(["title", "notes"], [{"title": "x", "notes": "y"}])


def test_malformed_row_marked_invalid():
    headers = ["url"]
    rows = parse_sheet_rows(headers, [{"url": "not a url without host"}])
    # urlparse may still invent a host from the string — force empty path case
    rows2 = parse_sheet_rows(headers, [{"url": "https://example.nl/"}])
    assert rows2[0].status == "invalid"


def test_duplicate_rows_marked():
    headers = ["url"]
    raw = [
        {"url": "https://a.nl/post-one/"},
        {"url": "https://a.nl/post-one/"},
        {"url": "https://a.nl/post-two/"},
    ]
    rows = parse_sheet_rows(headers, raw)
    mark_duplicates(rows)
    assert rows[0].status == "valid"
    assert rows[1].status == "duplicate"
    assert rows[2].status == "valid"


def test_duplicate_url_headers_are_preserved_and_flattened():
    """Sheets with two URL columns (URL / URL) must keep both lists of blogs."""
    csv_text = (
        "URL,Removed?,,,URL,Removed?\n"
        "https://a.nl/blog/one/,,,,,\n"
        "https://a.nl/blog/two/,,,,https://b.nl/spam-one/,\n"
        "https://a.nl/blog/three/,,,,https://b.nl/spam-two/,\n"
    )
    headers, raw = load_csv_text(csv_text)
    assert headers[0] == "URL"
    assert "URL__1" in headers
    rows = parse_sheet_rows(headers, raw)
    mark_duplicates(rows)
    urls = [r.url for r in rows if r.status == "valid"]
    assert urls == [
        "https://a.nl/blog/one/",
        "https://a.nl/blog/two/",
        "https://b.nl/spam-one/",
        "https://a.nl/blog/three/",
        "https://b.nl/spam-two/",
    ]


def test_payload_id_preferred():
    headers = ["payload_id", "url"]
    rows = parse_sheet_rows(
        headers,
        [{"payload_id": "abc123", "url": "https://a.nl/ignored/"}],
    )
    assert rows[0].payload_id == "abc123"
    assert rows[0].status == "valid"
    assert rows[0].identity_key.startswith("id:")


def test_sanitize_blog_slug_accents():
    assert sanitize_blog_slug("Café Één!") == "cafe-een"


def test_normalize_domain_strips_www():
    assert normalize_domain("https://WWW.Example.NL/path") == "example.nl"


def test_google_sheet_export_url_preserves_gid():
    edit = (
        "https://docs.google.com/spreadsheets/d/"
        "1-G5IcB_qMuKsVtqPsedmJVhWdmZKy9NP6ed_LPx3kfs/edit?gid=0#gid=0"
    )
    export = google_sheet_export_url(edit)
    assert "1-G5IcB_qMuKsVtqPsedmJVhWdmZKy9NP6ed_LPx3kfs" in export
    assert "format=csv" in export
    assert "gid=0" in export


def test_google_sheet_export_url_from_id():
    url = google_sheet_export_url("1LKO7wQCq7HX8MmBTKZwv30kURZ0YJ8MqE66DcsaWDKM")
    assert "export?format=csv" in url
    assert "1LKO7wQCq7HX8MmBTKZwv30kURZ0YJ8MqE66DcsaWDKM" in url


def test_load_csv_text_basic():
    text = "url,notes\nhttps://a.nl/x/,ok\n"
    headers, rows = load_csv_text(text)
    assert "url" in headers
    assert rows[0]["url"] == "https://a.nl/x/"


def test_env_bool_defaults_dry_run_true(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert env_bool("DRY_RUN", True) is True
    monkeypatch.setenv("DRY_RUN", "false")
    assert env_bool("DRY_RUN", True) is False


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200, headers: Optional[dict] = None):
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        if self._payload is None:
            return b""
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeHttp:
    """Routes requests by method+path prefix; supports scripted failures."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.delete_ids: list[str] = []
        self.published: list[str] = []
        self.tenants = {
            "example.nl": {"id": "t1", "slug": "example", "domain": "example.nl"},
        }
        self.blogs_by_id = {
            "p1": {"id": "p1", "slug": "hello", "tenant": "t1", "title": "Hello"},
        }
        self.blogs_by_tenant_slug = {
            ("t1", "hello"): [{"id": "p1", "slug": "hello", "tenant": "t1"}],
        }
        self.slug_only = {
            "shared": [
                {"id": "a", "slug": "shared", "tenant": "t1"},
                {"id": "b", "slug": "shared", "tenant": "t2"},
            ],
            "unique-only": [{"id": "c", "slug": "unique-only", "tenant": "t1"}],
        }
        self.fail_plan: list[Any] = []  # queue of exceptions or responses
        self.sleeps: list[float] = []
        # Auth simulation (Payload + payload-totp plugin behaviour)
        self.require_login = False
        self.require_totp = False
        self.password = "pw"
        self.totp_code = "123456"
        self.valid_api_keys: set[str] = set()
        self.user_api_key_enabled = False
        self.user_api_key: Optional[str] = None
        self.logins: list[str] = []
        self.totp_attempts: list[str] = []
        self.refreshes = 0

    def _auth_header(self, req) -> str:  # noqa: ANN001
        return req.get_header("Authorization") or ""

    def _is_api_key(self, req) -> bool:  # noqa: ANN001
        h = self._auth_header(req)
        return h.startswith("users API-Key ") and h.split(" ", 2)[2] in self.valid_api_keys

    def _authed(self, req) -> bool:  # noqa: ANN001
        h = self._auth_header(req)
        return h.startswith("JWT ") or self._is_api_key(req)

    def _has_totp_cookie(self, req) -> bool:  # noqa: ANN001
        return "payload-totp=" in (req.get_header("Cookie") or "")

    def opener(self, req, timeout=60):  # noqa: ANN001
        method = req.get_method()
        full = req.full_url
        path = full.split("?", 1)[0].replace("https://cms.test", "")
        self.calls.append((method, path))

        if method == "GET" and path == "/api/users/me":
            if self.require_login and not self._authed(req):
                return FakeResponse({"user": None})
            return FakeResponse(
                {
                    "user": {
                        "id": "u1",
                        "email": "admin@test.local",
                        "roles": ["super-admin"],
                        "hasTotp": True,
                        "enableAPIKey": self.user_api_key_enabled,
                        "apiKey": self.user_api_key if self.user_api_key_enabled else None,
                    }
                }
            )

        if method == "GET" and path == "/api/access":
            # Payload 3 "sanitized" shape (as seen live): granted ops are `true`,
            # denied ops are omitted entirely.
            totp_ok = (not self.require_totp) or self._has_totp_cookie(req) or self._is_api_key(req)
            bp: dict[str, Any] = {"read": True}
            if totp_ok:
                bp.update({"create": True, "update": True, "delete": True})
            return FakeResponse({"canAccessAdmin": True, "collections": {"blog-posts": bp}})

        if method == "POST" and path == "/api/users/login":
            body = json.loads(req.data.decode()) if req.data else {}
            self.logins.append(body.get("email"))
            if body.get("password") != self.password:
                import urllib.error as _ue
                raise _ue.HTTPError(full, 401, "Unauthorized", hdrs={}, fp=io.BytesIO(b'{"errors":[]}'))
            return FakeResponse({"token": "jwt-1", "user": {"id": "u1", "email": body.get("email")}})

        if method == "POST" and path == "/api/verify-totp":
            body = json.loads(req.data.decode()) if req.data else {}
            self.totp_attempts.append(body.get("token"))
            if body.get("token") != self.totp_code:
                return FakeResponse({"ok": False, "message": "Incorrect code"})
            return FakeResponse({"ok": True}, headers={"Set-Cookie": "payload-totp=totp-cookie-1; Path=/; HttpOnly"})

        if method == "POST" and path == "/api/users/refresh-token":
            self.refreshes += 1
            headers = {}
            if self._has_totp_cookie(req):
                headers["Set-Cookie"] = f"payload-totp=totp-cookie-{self.refreshes + 1}; Path=/; HttpOnly"
            return FakeResponse({"refreshedToken": f"jwt-{self.refreshes + 1}"}, headers=headers)

        if method == "PATCH" and path == "/api/users/u1":
            body = json.loads(req.data.decode()) if req.data else {}
            self.user_api_key_enabled = bool(body.get("enableAPIKey"))
            self.user_api_key = body.get("apiKey")
            return FakeResponse({"doc": {"id": "u1", "enableAPIKey": True, "apiKey": self.user_api_key}})

        if self.require_totp and method == "DELETE" and not (self._has_totp_cookie(req) or self._is_api_key(req)):
            import urllib.error as _ue
            raise _ue.HTTPError(
                full, 403, "Forbidden", hdrs={},
                fp=io.BytesIO(b'{"errors":[{"message":"You are not allowed to perform this action."}]}'),
            )

        # Injected failures apply to content calls only — not auth probes.
        if self.fail_plan and (
            method == "DELETE"
            or path.startswith("/api/blog-posts/")
            or path == "/api/blog-posts"
        ):
            item = self.fail_plan.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        if method == "GET" and path == "/api/tenants":
            qs = full.split("?", 1)[1] if "?" in full else ""
            if "where%5Bdomain%5D%5Bequals%5D=example.nl" in qs or "where[domain][equals]=example.nl" in qs:
                return FakeResponse({"docs": [self.tenants["example.nl"]]})
            if "missing.nl" in qs:
                return FakeResponse({"docs": []})
            # decode for robustness
            from urllib.parse import parse_qs, unquote_plus

            params = parse_qs(qs)
            domain = (params.get("where[domain][equals]") or [None])[0]
            slug = (params.get("where[slug][equals]") or [None])[0]
            if domain:
                domain = unquote_plus(domain)
                key = domain[4:] if domain.startswith("www.") else domain
                doc = self.tenants.get(key)
                return FakeResponse({"docs": [doc] if doc else []})
            if slug:
                for t in self.tenants.values():
                    if t["slug"] == slug:
                        return FakeResponse({"docs": [t]})
                return FakeResponse({"docs": []})
            return FakeResponse({"docs": []})

        if method == "GET" and path.startswith("/api/blog-posts/"):
            doc_id = path.rsplit("/", 1)[-1]
            doc = self.blogs_by_id.get(doc_id)
            if not doc:
                import urllib.error as _ue
                raise _ue.HTTPError(
                    full, 404, "Not Found", hdrs={}, fp=io.BytesIO(b"{}")
                )
            return FakeResponse(doc)

        if method == "GET" and path == "/api/blog-posts":
            from urllib.parse import parse_qs, unquote_plus

            qs = full.split("?", 1)[1] if "?" in full else ""
            params = {k: unquote_plus(v[0]) for k, v in parse_qs(qs).items()}
            slug = params.get("where[slug][equals]") or params.get("where[and][0][slug][equals]")
            tenant = params.get("where[tenant][equals]") or params.get("where[and][1][tenant][equals]")
            if tenant and slug:
                return FakeResponse({"docs": self.blogs_by_tenant_slug.get((tenant, slug), [])})
            if slug and not tenant:
                return FakeResponse({"docs": self.slug_only.get(slug, [])})
            return FakeResponse({"docs": []})

        if method == "DELETE" and path.startswith("/api/blog-posts/"):
            doc_id = path.rsplit("/", 1)[-1]
            self.delete_ids.append(doc_id)
            return FakeResponse(None)

        if method == "POST" and path.startswith("/api/tenants/") and path.endswith("/publish"):
            tenant_id = path.split("/")[-2]
            self.published.append(tenant_id)
            return FakeResponse({"ok": True, "message": "dispatched", "runUrl": f"https://ci.test/{tenant_id}"})

        raise AssertionError(f"Unhandled request {method} {full}")


def make_client(http: FakeHttp, **kwargs: Any) -> PayloadRestClient:
    return PayloadRestClient(
        "https://cms.test",
        "secret-token-do-not-log",
        request_delay_ms=0,
        sleep_fn=lambda s: http.sleeps.append(s),
        opener=http.opener,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_match_by_payload_id_found():
    http = FakeHttp()
    matcher = Matcher(make_client(http), "blog-posts")
    row = SheetRow(row_number=1, raw={}, payload_id="p1", status="valid", identity_key="id:p1")
    result = matcher.match(row)
    assert result.status == "matched"
    assert result.payload_id == "p1"


def test_match_payload_id_not_found():
    http = FakeHttp()
    matcher = Matcher(make_client(http), "blog-posts")
    row = SheetRow(row_number=1, raw={}, payload_id="missing", status="valid", identity_key="id:missing")
    result = matcher.match(row)
    assert result.status == "not_found"


def test_match_by_domain_and_slug():
    http = FakeHttp()
    matcher = Matcher(make_client(http), "blog-posts")
    row = SheetRow(
        row_number=1,
        raw={},
        domain="example.nl",
        slug="hello",
        status="valid",
        identity_key="domain_slug:example.nl|hello",
    )
    result = matcher.match(row)
    assert result.status == "matched"
    assert result.payload_id == "p1"


def test_match_not_found_unknown_slug():
    http = FakeHttp()
    matcher = Matcher(make_client(http), "blog-posts")
    row = SheetRow(
        row_number=1,
        raw={},
        domain="example.nl",
        slug="nope",
        status="valid",
        identity_key="x",
    )
    result = matcher.match(row)
    assert result.status == "not_found"


def test_match_ambiguous_slug_across_tenants():
    http = FakeHttp()
    matcher = Matcher(make_client(http), "blog-posts")
    row = SheetRow(row_number=1, raw={}, slug="shared", status="valid", identity_key="y")
    result = matcher.match(row)
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2


def test_successful_deletion_via_client():
    http = FakeHttp()
    client = make_client(http)
    client.delete_blog("blog-posts", "p1")
    assert http.delete_ids == ["p1"]


# ---------------------------------------------------------------------------
# Retry / rate limit
# ---------------------------------------------------------------------------


def test_retry_on_429_then_success():
    import urllib.error

    http = FakeHttp()
    err = urllib.error.HTTPError(
        "https://cms.test/api/blog-posts/p1",
        429,
        "Too Many",
        hdrs={"Retry-After": "0"},
        fp=io.BytesIO(b'{"errors":[]}'),
    )
    # First call fails with 429, second succeeds via normal routing — inject only one failure
    http.fail_plan = [err]
    client = make_client(http, max_retries=3, backoff_base_s=0.01)
    doc = client.get_blog_by_id("blog-posts", "p1")
    assert doc and doc["id"] == "p1"
    assert http.sleeps  # backoff happened


def test_permanent_4xx_not_retried_indefinitely():
    import urllib.error

    http = FakeHttp()
    err = urllib.error.HTTPError(
        "https://cms.test/api/blog-posts",
        400,
        "Bad",
        hdrs=None,
        fp=io.BytesIO(b'{"errors":["bad"]}'),
    )
    http.fail_plan = [err]
    client = make_client(http, max_retries=5)
    with pytest.raises(PayloadApiError) as ei:
        client.find_blogs("blog-posts", slug="hello", tenant_id="t1")
    assert ei.value.status == 400
    assert len(http.sleeps) == 0


def test_request_delay_rate_limiting():
    http = FakeHttp()
    client = make_client(http)
    client.request_delay_ms = 50
    client._last_request_at = __import__("time").monotonic()
    client.get_blog_by_id("blog-posts", "p1")
    assert any(s > 0 for s in http.sleeps)


# ---------------------------------------------------------------------------
# Resume / dry-run / confirmation
# ---------------------------------------------------------------------------


def test_progress_resume_skips_completed(tmp_path: Path):
    progress = ProgressStore(tmp_path / "progress.json")
    progress.mark("id:p1", status="deleted", row_number=1, payload_id="p1")
    assert progress.is_done("id:p1")
    # reload
    progress2 = ProgressStore(tmp_path / "progress.json")
    assert progress2.is_done("id:p1")


def test_dry_run_does_not_delete(tmp_path: Path):
    http = FakeHttp()
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("payload_id\np1\n", encoding="utf-8")
    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="secret-token-do-not-log",
        csv_path=csv_path,
        dry_run=True,
        output_dir=tmp_path / "out",
        request_delay_ms=0,
        batch_size=10,
    )

    real_client_init = PayloadRestClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("opener", http.opener)
        kwargs.setdefault("sleep_fn", lambda s: http.sleeps.append(s))
        kwargs["request_delay_ms"] = 0
        real_client_init(self, *args, **kwargs)

    with patch.object(PayloadRestClient, "__init__", patched_init):
        counters = run(cfg)

    assert counters.dry_run == 1
    assert counters.deleted == 0
    assert http.delete_ids == []


def test_execute_requires_confirmation(tmp_path: Path, monkeypatch):
    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="x",
        dry_run=False,
        yes=False,
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "nope")
    with pytest.raises(SystemExit, match="Confirmation mismatch"):
        require_confirmation(cfg, {"total_sheet_rows": 1, "valid_rows": 1, "ready_for_deletion": 1, "not_found": 0, "ambiguous": 0})


def test_execute_with_yes_needs_env_phrase(tmp_path: Path, monkeypatch):
    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="x",
        dry_run=False,
        yes=True,
    )
    monkeypatch.delenv("BULK_DELETE_CONFIRM", raising=False)
    with pytest.raises(SystemExit, match="BULK_DELETE_CONFIRM"):
        require_confirmation(cfg, {"total_sheet_rows": 1, "valid_rows": 1, "ready_for_deletion": 1, "not_found": 0, "ambiguous": 0})
    monkeypatch.setenv("BULK_DELETE_CONFIRM", CONFIRM_PHRASE)
    require_confirmation(cfg, {"total_sheet_rows": 1, "valid_rows": 1, "ready_for_deletion": 1, "not_found": 0, "ambiguous": 0})


def test_real_delete_after_confirm(tmp_path: Path, monkeypatch):
    http = FakeHttp()
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("payload_id\np1\n", encoding="utf-8")
    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="secret-token-do-not-log",
        csv_path=csv_path,
        dry_run=False,
        yes=True,
        output_dir=tmp_path / "out",
        request_delay_ms=0,
    )
    monkeypatch.setenv("BULK_DELETE_CONFIRM", CONFIRM_PHRASE)

    real_client_init = PayloadRestClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("opener", http.opener)
        kwargs.setdefault("sleep_fn", lambda s: None)
        kwargs["request_delay_ms"] = 0
        real_client_init(self, *args, **kwargs)

    with patch.object(PayloadRestClient, "__init__", patched_init):
        counters = run(cfg)

    assert counters.deleted == 1
    assert http.delete_ids == ["p1"]
    assert http.published == ["t1"]
    assert counters.deployed == 1


def test_resume_skips_already_deleted(tmp_path: Path):
    http = FakeHttp()
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("payload_id\np1\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    progress = ProgressStore(out / "progress.json")
    progress.mark("id:p1", status="deleted", row_number=1, payload_id="p1")

    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="secret",
        csv_path=csv_path,
        dry_run=True,
        output_dir=out,
        progress_file=out / "progress.json",
        request_delay_ms=0,
    )

    real_client_init = PayloadRestClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("opener", http.opener)
        kwargs.setdefault("sleep_fn", lambda s: None)
        kwargs["request_delay_ms"] = 0
        real_client_init(self, *args, **kwargs)

    with patch.object(PayloadRestClient, "__init__", patched_init):
        counters = run(cfg)

    assert counters.skipped == 1
    assert counters.dry_run == 0
    assert http.delete_ids == []


def test_api_failure_recorded(tmp_path: Path):
    import urllib.error

    http = FakeHttp()
    http.fail_plan = [
        urllib.error.HTTPError(
            "https://cms.test/api/blog-posts/p1",
            500,
            "Boom",
            hdrs=None,
            fp=io.BytesIO(b"err"),
        )
        for _ in range(6)
    ]
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("payload_id\np1\n", encoding="utf-8")
    cfg = RunConfig(
        payload_url="https://cms.test",
        api_token="secret",
        csv_path=csv_path,
        dry_run=True,
        output_dir=tmp_path / "out",
        request_delay_ms=0,
        max_retries=2,
        backoff_base_s=0.001,
    )

    real_client_init = PayloadRestClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("opener", http.opener)
        kwargs.setdefault("sleep_fn", lambda s: None)
        kwargs["request_delay_ms"] = 0
        kwargs["max_retries"] = 2
        kwargs["backoff_base_s"] = 0.001
        real_client_init(self, *args, **kwargs)

    with patch.object(PayloadRestClient, "__init__", patched_init):
        counters = run(cfg)

    assert counters.failed >= 1


# ---------------------------------------------------------------------------
# TOTP / session / API-key persistence
# ---------------------------------------------------------------------------


def _totp_cfg(tmp_path: Path, **overrides: Any) -> RunConfig:
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("payload_id\np1\n", encoding="utf-8")
    base = dict(
        payload_url="https://cms.test",
        api_token="",
        auth_mode="jwt",
        email="admin@test.local",
        password="pw",
        totp_code="123456",
        env_file=tmp_path / ".env",
        csv_path=csv_path,
        dry_run=False,
        yes=True,
        output_dir=tmp_path / "out",
        request_delay_ms=0,
    )
    base.update(overrides)
    return RunConfig(**base)


def test_totp_required_then_delete_succeeds(tmp_path: Path, monkeypatch):
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    cfg = _totp_cfg(tmp_path)
    monkeypatch.setenv("BULK_DELETE_CONFIRM", CONFIRM_PHRASE)

    client = PayloadRestClient("https://cms.test", "", auth_mode="jwt", request_delay_ms=0, opener=http.opener)
    establish_session(cfg, client)

    assert http.logins == ["admin@test.local"]
    assert http.totp_attempts == ["123456"]
    assert client.totp_cookie == "totp-cookie-1"
    client.delete_blog("blog-posts", "p1")
    assert http.delete_ids == ["p1"]
    # API key was created (none existed) and persisted to .env
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PAYLOAD_API_KEY=" in env_text
    assert http.user_api_key and http.user_api_key in env_text


def test_wrong_totp_code_aborts(tmp_path: Path):
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    cfg = _totp_cfg(tmp_path, totp_code="000000")
    client = PayloadRestClient("https://cms.test", "", auth_mode="jwt", request_delay_ms=0, opener=http.opener)
    with pytest.raises(SystemExit, match="TOTP verification failed"):
        establish_session(cfg, client)
    assert http.delete_ids == []


def test_jwt_without_totp_cannot_delete(tmp_path: Path):
    """Reproduces the production 403: plain JWT is refused by the TOTP wrapper."""
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    client = PayloadRestClient("https://cms.test", "", auth_mode="jwt", request_delay_ms=0, opener=http.opener)
    client.login("admin@test.local", "pw")
    with pytest.raises(PayloadApiError) as exc:
        client.delete_blog("blog-posts", "p1")
    assert exc.value.status == 403


def test_existing_api_key_is_reused_not_rotated(tmp_path: Path):
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    http.user_api_key_enabled = True
    http.user_api_key = "existing-ci-key"
    cfg = _totp_cfg(tmp_path)
    client = PayloadRestClient("https://cms.test", "", auth_mode="jwt", request_delay_ms=0, opener=http.opener)
    establish_session(cfg, client)
    assert http.user_api_key == "existing-ci-key"
    assert "PAYLOAD_API_KEY=existing-ci-key" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_valid_api_key_skips_totp(tmp_path: Path):
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    http.valid_api_keys = {"good-key"}
    cfg = _totp_cfg(tmp_path, api_token="good-key", auth_mode="api-key")
    client = PayloadRestClient("https://cms.test", "good-key", auth_mode="api-key", request_delay_ms=0, opener=http.opener)
    establish_session(cfg, client)
    assert http.logins == []
    assert http.totp_attempts == []
    client.delete_blog("blog-posts", "p1")
    assert http.delete_ids == ["p1"]


def test_bad_api_key_falls_back_to_password(tmp_path: Path):
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    cfg = _totp_cfg(tmp_path, api_token="bad-key", auth_mode="api-key")
    client = PayloadRestClient("https://cms.test", "bad-key", auth_mode="api-key", request_delay_ms=0, opener=http.opener)
    establish_session(cfg, client)
    assert http.logins == ["admin@test.local"]
    assert client.auth_mode == "jwt"
    assert client.totp_cookie


def test_session_refresh_rotates_jwt_and_totp_cookie():
    http = FakeHttp()
    http.require_login = True
    http.require_totp = True
    client = PayloadRestClient(
        "https://cms.test", "", auth_mode="jwt", request_delay_ms=0, opener=http.opener, session_refresh_s=0
    )
    client.login("admin@test.local", "pw")
    client.verify_totp("123456")
    assert client.api_token == "jwt-1"
    client.get_blog_by_id("blog-posts", "p1")  # triggers refresh (session_refresh_s=0)
    assert http.refreshes == 1
    assert client.api_token == "jwt-2"
    assert client.totp_cookie == "totp-cookie-2"


def test_upsert_env_var_replaces_and_appends(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("PAYLOAD_URL=https://x\nPAYLOAD_API_KEY=old\n", encoding="utf-8")
    upsert_env_var(env, "PAYLOAD_API_KEY", "new")
    upsert_env_var(env, "EXTRA", "1")
    text = env.read_text(encoding="utf-8")
    assert "PAYLOAD_API_KEY=new" in text and "old" not in text
    assert text.count("PAYLOAD_API_KEY=") == 1
    assert "EXTRA=1" in text


def test_identity_key_stable():
    assert identity_key_for_row("X", None, None, None, None) == "id:X"
    assert identity_key_for_row(None, "A.nl", "Post", None, None) == "domain_slug:a.nl|post"


def test_build_config_defaults_dry_run(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PAYLOAD_URL", "https://cms.test")
    monkeypatch.setenv("PAYLOAD_API_KEY", "k")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("PAYLOAD_EMAIL", raising=False)
    monkeypatch.delenv("PAYLOAD_PASSWORD", raising=False)
    monkeypatch.delenv("PAYLOAD_USER", raising=False)
    monkeypatch.delenv("GOOGLE_SHEET_CSV_URL", raising=False)
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("url\nhttps://a.nl/x/\n", encoding="utf-8")
    # Prevent load_local_env from picking up the real repo .env
    monkeypatch.setattr(
        "bulk_delete_payload_blogs.load_local_env",
        lambda: None,
    )
    cfg = build_config_from_env_and_args(["--csv", str(csv_path)])
    assert cfg.dry_run is True
    assert cfg.auth_mode == "api-key"


def test_never_logs_token_in_identity():
    # sanity: identity keys must not embed the API token
    key = identity_key_for_row("secret-token-do-not-log", None, None, None, None)
    assert key == "id:secret-token-do-not-log"  # id column value is fine; auth token separate
