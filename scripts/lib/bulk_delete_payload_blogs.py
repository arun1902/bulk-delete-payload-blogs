"""
Safe, resumable bulk deletion of Payload `blog-posts` listed in a Google Sheet / CSV.

Matching (inspected from apps/payload schema - do NOT assume sourceUrl):
  1. Exact Payload document ID (preferred)
  2. Tenant domain + blog slug (from full URL or separate columns)
  3. Tenant slug + blog slug

Never deletes by domain/tenant alone. Never uses delete-all. Never touches Postgres.

Auth (same as existing scripts / payload-sdk):
  Authorization: users API-Key <PAYLOAD_API_KEY|PAYLOAD_API_TOKEN>

Endpoints:
  GET  /api/tenants?where[domain|slug][equals]=...
  GET  /api/blog-posts?where[...]&limit=2&depth=0&select[...]
  GET  /api/blog-posts/:id
  DELETE /api/blog-posts/:id
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

DEFAULT_COLLECTION = "blog-posts"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "bulk-delete-output"
CONFIRM_PHRASE = "DELETE"

NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 405, 409, 410, 422})

ID_COLUMNS = ("payload_id", "payloadid", "id", "document_id", "doc_id", "blog_id")
URL_COLUMNS = (
    "blog_url",
    "url",
    "link",
    "canonical_url",
    "canonicalurl",
    "source_url",
    "sourceurl",
    "post_url",
    "page_url",
)
DOMAIN_COLUMNS = ("domain", "website", "host", "site", "site_domain")
SLUG_COLUMNS = ("slug", "blog_slug", "post_slug", "path_slug")
TENANT_SLUG_COLUMNS = ("tenant_slug", "tenant", "site_slug")
KNOWN_PATH_PREFIXES = ("blog", "blogs", "posts", "post", "articles", "article", "nieuws", "news")

logger = logging.getLogger("bulk_delete_payload_blogs")


@dataclass
class SheetRow:
    row_number: int
    raw: dict[str, str]
    payload_id: Optional[str] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    slug: Optional[str] = None
    tenant_slug: Optional[str] = None
    identity_key: str = ""
    status: str = "invalid"
    error: Optional[str] = None
    matched_id: Optional[str] = None
    matched_slug: Optional[str] = None
    matched_tenant: Optional[str] = None


@dataclass
class MatchResult:
    status: str
    payload_id: Optional[str] = None
    slug: Optional[str] = None
    tenant: Optional[str] = None
    error: Optional[str] = None
    candidates: list[str] = field(default_factory=list)


@dataclass
class RunConfig:
    payload_url: str
    api_token: str
    auth_mode: str = "api-key"  # "api-key" | "jwt"
    email: Optional[str] = None
    password: Optional[str] = None
    totp_code: Optional[str] = None
    save_api_key: bool = True
    env_file: Optional[Path] = None
    collection: str = DEFAULT_COLLECTION
    csv_path: Optional[Path] = None
    sheet_csv_url: Optional[str] = None
    blog_url_column: Optional[str] = None
    dry_run: bool = True
    limit: Optional[int] = None
    batch_size: int = 25
    request_delay_ms: int = 100
    max_retries: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    output_dir: Path = DEFAULT_OUTPUT_DIR
    yes: bool = False
    progress_file: Optional[Path] = None
    results_file: Optional[Path] = None
    failed_file: Optional[Path] = None
    summary_file: Optional[Path] = None
    retry_file: Optional[Path] = None
    log_file: Optional[Path] = None


@dataclass
class Counters:
    total_rows: int = 0
    valid_rows: int = 0
    unique_rows: int = 0
    duplicate_rows: int = 0
    invalid_rows: int = 0
    matched: int = 0
    not_found: int = 0
    ambiguous: int = 0
    deleted: int = 0
    dry_run: int = 0
    failed: int = 0
    skipped: int = 0
    remaining: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")


def pick_column(
    headers: Sequence[str],
    aliases: Sequence[str],
    explicit: Optional[str] = None,
) -> Optional[str]:
    normalized = {normalize_header(h): h for h in headers}
    if explicit:
        key = normalize_header(explicit)
        if key in normalized:
            return normalized[key]
        if explicit in headers:
            return explicit
        raise ValueError(f"Column {explicit!r} not found in sheet headers: {list(headers)}")
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def normalize_domain(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split(":")[0]
    if v.startswith("www."):
        v = v[4:]
    return v


def sanitize_blog_slug(raw: str) -> str:
    """Mirror packages/payload-sdk sanitizeBlogSlug."""
    if not raw:
        return ""
    s = unicodedata.normalize("NFKD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def parse_blog_url(url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    raw = url.strip()
    if not raw:
        return None, None, "empty URL"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"unparseable URL: {exc}"
    host = parsed.hostname or ""
    domain = normalize_domain(host) if host else None
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return domain, None, "URL has no path slug"
    slug = sanitize_blog_slug(urllib.parse.unquote(parts[-1]))
    if not slug:
        return domain, None, "empty slug after sanitize"
    if len(parts) == 1 and slug in KNOWN_PATH_PREFIXES:
        return domain, None, f"URL path looks like a listing page (/{slug}), not a post"
    return domain, slug, None


def identity_key_for_row(
    payload_id: Optional[str],
    domain: Optional[str],
    slug: Optional[str],
    tenant_slug: Optional[str],
    url: Optional[str],
) -> str:
    if payload_id:
        return f"id:{payload_id.strip()}"
    if domain and slug:
        return f"domain_slug:{normalize_domain(domain)}|{sanitize_blog_slug(slug)}"
    if tenant_slug and slug:
        return f"tenant_slug:{tenant_slug.strip().lower()}|{sanitize_blog_slug(slug)}"
    if url:
        return f"url:{url.strip().lower().rstrip('/')}"
    digest = hashlib.sha1(repr((payload_id, domain, slug, tenant_slug, url)).encode()).hexdigest()[:12]
    return f"invalid:{digest}"


def uniquify_headers(fieldnames: Sequence[Optional[str]]) -> list[str]:
    """Make duplicate CSV headers unique (URL, URL -> URL, URL__1)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in fieldnames:
        base = raw if raw is not None else ""
        count = seen.get(base, 0)
        seen[base] = count + 1
        out.append(base if count == 0 else f"{base}__{count}")
    return out


def load_csv_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    # Use plain reader so duplicate header names are not collapsed by DictReader.
    rows_iter = csv.reader(text.splitlines(), dialect=dialect)
    try:
        raw_headers = next(rows_iter)
    except StopIteration as exc:
        raise ValueError("CSV has no header row") from exc
    headers = uniquify_headers(raw_headers)
    if not any(h.strip() for h in headers):
        raise ValueError("CSV has no header row")
    rows: list[dict[str, str]] = []
    for values in rows_iter:
        row: dict[str, str] = {}
        for idx, key in enumerate(headers):
            row[key] = values[idx] if idx < len(values) and values[idx] is not None else ""
        rows.append(row)
    return headers, rows


def find_url_columns(
    headers: Sequence[str],
    explicit: Optional[str] = None,
) -> list[str]:
    """Return all URL-like columns (supports duplicated headers renamed to URL__1)."""
    if explicit:
        col = pick_column(headers, URL_COLUMNS, explicit=explicit)
        return [col] if col else []
    cols: list[str] = []
    for h in headers:
        key = normalize_header(h)
        base = re.sub(r"__\d+$", "", key)
        if base in URL_COLUMNS:
            cols.append(h)
    return cols


def fetch_url_text(url: str, timeout: float = 120.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "astropayload-bulk-delete/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def google_sheet_export_url(sheet_id_or_url: str) -> str:
    s = sheet_id_or_url.strip()
    if "export?format=csv" in s or s.endswith(".csv"):
        return s
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    gid_m = re.search(r"(?:[#&?]gid=)(\d+)", s)
    if m:
        export = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
        if gid_m:
            export += f"&gid={gid_m.group(1)}"
        return export
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", s):
        return f"https://docs.google.com/spreadsheets/d/{s}/export?format=csv"
    return s


class PayloadApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def browser_headers() -> dict[str, str]:
    # Browser-like UA avoids Cloudflare Error 1010 (browser_signature_banned)
    # on some production hosts (e.g. payload.10beste.com).
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }


def normalize_api_key(raw: str) -> str:
    """Strip quotes/BOM and accidental 'users API-Key ' prefixes from .env values."""
    key = raw.strip().lstrip("\ufeff").strip().strip("'").strip('"').strip()
    for prefix in ("users API-Key ", "users API-key ", "API-Key ", "Bearer ", "JWT "):
        if key.lower().startswith(prefix.lower()):
            key = key[len(prefix) :].strip()
    return key


def _cookie_from_set_cookie(headers: Any, name: str) -> Optional[str]:
    """Extract a cookie value from Set-Cookie response headers."""
    if headers is None:
        return None
    values: list[str] = []
    if hasattr(headers, "get_all"):
        values = headers.get_all("Set-Cookie") or []
    elif isinstance(headers, dict):
        raw = headers.get("Set-Cookie")
        if raw:
            values = [raw] if isinstance(raw, str) else list(raw)
    for sc in values:
        first = sc.split(";", 1)[0].strip()
        if "=" in first:
            k, v = first.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return None


class PayloadRestClient:
    """
    Auth modes:
      api-key : Authorization: users API-Key <key>   (exempt from TOTP, no expiry)
      jwt     : Authorization: JWT <token> [+ Cookie: payload-totp=<cookie>]
                Payload's TOTP plugin only allows create/update/delete for a
                TOTP-verified session or an API key, so jwt mode needs
                verify_totp() before any deletion, and refresh_session() to
                stay alive on long runs.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        auth_mode: str = "api-key",
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        request_delay_ms: int = 100,
        sleep_fn: Callable[[float], None] = time.sleep,
        opener: Optional[Callable[..., Any]] = None,
        cookie_prefix: str = "payload",
        session_refresh_s: int = 40 * 60,
        request_timeout_s: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.auth_mode = auth_mode  # "api-key" | "jwt"
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.request_delay_ms = request_delay_ms
        self.sleep_fn = sleep_fn
        self._opener = opener or urllib.request.urlopen
        self._last_request_at = 0.0
        self.cookie_prefix = cookie_prefix
        self.totp_cookie: Optional[str] = None
        self.session_refresh_s = session_refresh_s
        self.request_timeout_s = request_timeout_s
        self._session_started_at = time.monotonic()
        self._refreshing = False

    # ----------------------------------------------------------------- auth

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        headers = browser_headers()
        if not auth:
            return headers
        if self.auth_mode == "jwt":
            headers["Authorization"] = f"JWT {self.api_token}"
            if self.totp_cookie:
                headers["Cookie"] = f"{self.cookie_prefix}-totp={self.totp_cookie}"
        else:
            headers["Authorization"] = f"users API-Key {self.api_token}"
        return headers

    def login(self, email: str, password: str) -> dict[str, Any]:
        """POST /api/users/login → switch to jwt mode. Never logs credentials."""
        try:
            data, _ = self._do_request(
                "POST",
                "/api/users/login",
                json_body={"email": email, "password": password},
                auth=False,
            )
        except PayloadApiError as exc:
            raise SystemExit(
                f"Login failed ({exc.status}). Check PAYLOAD_EMAIL / PAYLOAD_PASSWORD.\n"
                f"Server said: {exc.body[:300]}"
            ) from exc
        token = (data or {}).get("token") if isinstance(data, dict) else None
        if not token:
            raise SystemExit("Login succeeded but no token was returned.")
        self.api_token = str(token)
        self.auth_mode = "jwt"
        self.totp_cookie = None
        self._session_started_at = time.monotonic()
        return dict((data or {}).get("user") or {})

    def me(self) -> Optional[dict[str, Any]]:
        data = self.request("GET", "/api/users/me")
        user = (data or {}).get("user") if isinstance(data, dict) else None
        return dict(user) if user else None

    def collection_permissions(self, collection: str) -> dict[str, Optional[bool]]:
        """GET /api/access → {read, create, update, delete} booleans for a collection."""
        data = self.request("GET", "/api/access")
        collections = (data or {}).get("collections") or {}
        coll = collections.get(collection)
        out: dict[str, Optional[bool]] = {}
        for op in ("read", "create", "update", "delete"):
            if not isinstance(coll, dict):
                out[op] = None  # collection not visible at all
                continue
            perm = coll.get(op)
            if isinstance(perm, dict):  # legacy shape: {"permission": bool}
                out[op] = bool(perm.get("permission"))
            elif isinstance(perm, bool):  # Payload 3 sanitized shape
                out[op] = perm
            else:  # Payload 3 omits denied operations entirely
                out[op] = False
        return out

    def verify_totp(self, code: str) -> None:
        """POST /api/verify-totp with the 6-digit authenticator code.

        On success the plugin sets `<prefix>-totp`; we keep it and send it with
        every request so Payload treats this session as TOTP-verified.
        """
        if self.auth_mode != "jwt":
            raise SystemExit("verify_totp requires email/password (jwt) auth mode")
        code = re.sub(r"\D", "", code or "")
        if len(code) < 6:
            raise SystemExit("TOTP code must be 6 digits")
        data, headers = self._do_request("POST", "/api/verify-totp", json_body={"token": code})
        ok = bool(isinstance(data, dict) and data.get("ok"))
        cookie = _cookie_from_set_cookie(headers, f"{self.cookie_prefix}-totp")
        if not ok or not cookie:
            msg = (data or {}).get("message") if isinstance(data, dict) else ""
            raise SystemExit(
                f"TOTP verification failed: {msg or 'incorrect code'}. "
                "Open your authenticator app and re-run with a fresh code."
            )
        self.totp_cookie = cookie
        self._session_started_at = time.monotonic()
        logger.info("TOTP verified - session is now allowed to delete")

    def refresh_session(self) -> None:
        """POST /api/users/refresh-token → new JWT (+ new TOTP cookie via plugin hook)."""
        if self.auth_mode != "jwt" or self._refreshing:
            return
        self._refreshing = True
        try:
            data, headers = self._do_request("POST", "/api/users/refresh-token")
            token = (data or {}).get("refreshedToken") if isinstance(data, dict) else None
            if token:
                self.api_token = str(token)
            cookie = _cookie_from_set_cookie(headers, f"{self.cookie_prefix}-totp")
            if cookie:
                self.totp_cookie = cookie
            elif self.totp_cookie:
                logger.warning("refresh-token did not re-issue the TOTP cookie; deletes may start failing")
            self._session_started_at = time.monotonic()
            logger.info("Session refreshed")
        except PayloadApiError as exc:
            logger.warning("Session refresh failed (%s); continuing with current token", exc.status)
        finally:
            self._refreshing = False

    def _maybe_refresh(self) -> None:
        if self.auth_mode != "jwt" or self._refreshing:
            return
        if time.monotonic() - self._session_started_at >= self.session_refresh_s:
            self.refresh_session()

    def verify_auth(self) -> dict[str, Any]:
        """Fail fast if credentials do not authenticate. Never logs the secret."""
        try:
            user = self.me()
        except PayloadApiError as exc:
            raise SystemExit(
                "Auth check failed calling GET /api/users/me.\n"
                f"HTTP {exc.status}: {exc.body[:300]}\n"
                "Fix PAYLOAD_API_KEY / PAYLOAD_EMAIL+PAYLOAD_PASSWORD in .env (see .env.example)."
            ) from exc
        if not user:
            raise SystemExit(
                "AUTH FAILED: Payload did not accept your credentials "
                f"(auth_mode={self.auth_mode}).\n"
                "GET /api/users/me returned user=null.\n\n"
                "Put ONE of these in .env:\n"
                "  PAYLOAD_EMAIL=you@example.com\n"
                "  PAYLOAD_PASSWORD=your-password\n"
                "or a saved User API Key:\n"
                "  PAYLOAD_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n"
                "Then re-run: python scripts/bulk-delete-payload-blogs.py --limit 10"
            )
        # Prove collection read works (this is where 403 appeared before).
        try:
            self.request(
                "GET",
                "/api/tenants",
                query={"limit": "1", "depth": "0", "select[id]": "true"},
            )
        except PayloadApiError as exc:
            raise SystemExit(
                f"Authenticated as {user.get('email') or user.get('id')} but "
                f"GET /api/tenants returned {exc.status}.\n"
                "Your user likely lacks permission. Use a super-admin account.\n"
                f"Server: {exc.body[:300]}"
            ) from exc
        logger.info(
            "Auth OK as %s (roles=%s, mode=%s%s)",
            user.get("email") or user.get("id"),
            user.get("roles"),
            self.auth_mode,
            "+totp" if self.totp_cookie else "",
        )
        return user

    # ------------------------------------------------------------- transport

    def _throttle(self) -> None:
        if self.request_delay_ms <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        need = self.request_delay_ms / 1000.0
        if elapsed < need:
            self.sleep_fn(need - elapsed)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
    ) -> Any:
        self._maybe_refresh()
        data, _ = self._do_request(method, path, query=query, json_body=json_body)
        return data

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        auth: bool = True,
    ) -> tuple[Any, Any]:
        q = f"?{urllib.parse.urlencode(query)}" if query else ""
        url = f"{self.base_url}{path}{q}"
        body_bytes = None
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")

        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            req = urllib.request.Request(
                url, data=body_bytes, headers=self._headers(auth=auth), method=method
            )
            try:
                with self._opener(req, timeout=self.request_timeout_s) as resp:
                    self._last_request_at = time.monotonic()
                    resp_headers = getattr(resp, "headers", None)
                    raw = resp.read()
                    if not raw:
                        return None, resp_headers
                    return json.loads(raw.decode("utf-8")), resp_headers
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    err_body = ""
                status = exc.code
                retryable = status == 429 or status >= 500
                if status in NON_RETRYABLE_STATUS or (not retryable) or attempt > self.max_retries:
                    raise PayloadApiError(
                        f"{method} {path} -> {status}: {err_body[:400]}",
                        status=status,
                        body=err_body,
                    ) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after and str(retry_after).isdigit():
                    delay = float(retry_after)
                else:
                    delay = min(self.backoff_max_s, self.backoff_base_s * (2 ** (attempt - 1)))
                logger.warning(
                    "Retryable %s on %s %s (attempt %s/%s); sleeping %.1fs",
                    status,
                    method,
                    path,
                    attempt,
                    self.max_retries,
                    delay,
                )
                self.sleep_fn(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                self._last_request_at = time.monotonic()
                if attempt > self.max_retries:
                    raise PayloadApiError(f"{method} {path} network error: {exc}") from exc
                delay = min(self.backoff_max_s, self.backoff_base_s * (2 ** (attempt - 1)))
                logger.warning(
                    "Network error on %s %s (attempt %s/%s): %s; sleeping %.1fs",
                    method,
                    path,
                    attempt,
                    self.max_retries,
                    exc,
                    delay,
                )
                self.sleep_fn(delay)

    def find_tenant_by_domain(self, domain: str) -> Optional[dict[str, Any]]:
        d = normalize_domain(domain)
        for candidate in (d, f"www.{d}"):
            data = self.request(
                "GET",
                "/api/tenants",
                query={
                    "where[domain][equals]": candidate,
                    "limit": "1",
                    "depth": "0",
                    "select[id]": "true",
                    "select[slug]": "true",
                    "select[domain]": "true",
                },
            )
            docs = (data or {}).get("docs") or []
            if docs:
                return docs[0]
        return None

    def find_tenant_by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        data = self.request(
            "GET",
            "/api/tenants",
            query={
                "where[slug][equals]": slug.strip().lower(),
                "limit": "1",
                "depth": "0",
                "select[id]": "true",
                "select[slug]": "true",
                "select[domain]": "true",
            },
        )
        docs = (data or {}).get("docs") or []
        return docs[0] if docs else None

    def get_blog_by_id(self, collection: str, doc_id: str) -> Optional[dict[str, Any]]:
        try:
            return self.request(
                "GET",
                f"/api/{collection}/{urllib.parse.quote(str(doc_id), safe='')}",
                query={"depth": "0"},
            )
        except PayloadApiError as exc:
            if exc.status == 404:
                return None
            raise

    def find_blogs(
        self,
        collection: str,
        *,
        slug: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        lim = str(min(max(limit, 1), 25))
        if slug is not None and tenant_id is not None:
            query = {
                "limit": lim,
                "depth": "0",
                "select[id]": "true",
                "select[slug]": "true",
                "select[title]": "true",
                "select[tenant]": "true",
                "where[and][0][slug][equals]": slug,
                "where[and][1][tenant][equals]": str(tenant_id),
            }
        else:
            query = {
                "limit": lim,
                "depth": "0",
                "select[id]": "true",
                "select[slug]": "true",
                "select[title]": "true",
                "select[tenant]": "true",
            }
            if slug is not None:
                query["where[slug][equals]"] = slug
            if tenant_id is not None:
                query["where[tenant][equals]"] = str(tenant_id)
        data = self.request("GET", f"/api/{collection}", query=query)
        return list((data or {}).get("docs") or [])

    def delete_blog(self, collection: str, doc_id: str) -> None:
        self.request("DELETE", f"/api/{collection}/{urllib.parse.quote(str(doc_id), safe='')}")

    def ensure_api_key(self, user: Mapping[str, Any]) -> Optional[str]:
        """Return the account's API key, enabling one only if none exists.

        Never rotates an existing key (CI may depend on it).
        Requires a session that is allowed to update the user (TOTP or super-admin).
        """
        if user.get("enableAPIKey") and user.get("apiKey"):
            return str(user["apiKey"])
        user_id = user.get("id")
        if user_id is None:
            return None
        new_key = str(uuid.uuid4())
        try:
            data = self.request(
                "PATCH",
                f"/api/users/{urllib.parse.quote(str(user_id), safe='')}",
                json_body={"enableAPIKey": True, "apiKey": new_key},
            )
        except PayloadApiError as exc:
            logger.warning("Could not enable an API key on this account (%s)", exc.status)
            return None
        doc = (data or {}).get("doc") if isinstance(data, dict) else None
        if isinstance(doc, dict) and doc.get("enableAPIKey"):
            logger.info("Enabled a new API key on this account")
            return str(doc.get("apiKey") or new_key)
        return None


def upsert_env_var(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file (replace existing line or append). Value is never logged."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def prompt_totp_code(preset: Optional[str] = None) -> str:
    code = (preset or os.environ.get("PAYLOAD_TOTP_CODE") or "").strip()
    if code:
        return code
    non_interactive_msg = (
        "This Payload uses 2FA (TOTP). Deleting needs a TOTP-verified session.\n"
        "Run interactively, or pass --totp-code 123456 / PAYLOAD_TOTP_CODE, "
        "or save a User API Key as PAYLOAD_API_KEY (API keys bypass TOTP)."
    )
    if not sys.stdin.isatty():
        raise SystemExit(non_interactive_msg)
    print()
    print("This Payload uses 2FA. Deleting requires a one-time authenticator code.")
    try:
        return input("Enter the 6-digit code from your authenticator app: ").strip()
    except EOFError as exc:
        raise SystemExit(non_interactive_msg) from exc


def establish_session(cfg: "RunConfig", client: PayloadRestClient) -> dict[str, Any]:
    """
    Make `client` authenticated AND allowed to delete `cfg.collection`.

    1. Try PAYLOAD_API_KEY (exempt from TOTP).
    2. Else login with PAYLOAD_EMAIL/PASSWORD; if delete permission is missing,
       verify TOTP once; then persist the account API key to .env so the next
       run is unattended.
    """
    user: Optional[dict[str, Any]] = None

    if cfg.auth_mode == "api-key" and cfg.api_token:
        try:
            user = client.me()
        except PayloadApiError as exc:
            logger.warning("API key probe failed (%s)", exc.status)
            user = None
        if not user:
            if cfg.email and cfg.password:
                logger.warning("PAYLOAD_API_KEY was rejected - falling back to email/password login")
            else:
                client.verify_auth()  # raises with instructions

    if not user:
        if not (cfg.email and cfg.password):
            client.verify_auth()  # raises with instructions
        logger.info("Logging in with PAYLOAD_EMAIL / PAYLOAD_PASSWORD")
        client.login(cfg.email or "", cfg.password or "")
        user = client.me() or {}

    user = client.verify_auth()

    perms = client.collection_permissions(cfg.collection)
    logger.info("Permissions on %s: %s", cfg.collection, perms)
    if perms.get("delete") is True:
        return user

    if client.auth_mode == "api-key":
        raise SystemExit(
            f"API key authenticated as {user.get('email')} but has no delete permission "
            f"on {cfg.collection}. Use a super-admin account."
        )

    if not user.get("hasTotp"):
        raise SystemExit(
            f"{user.get('email')} has no delete permission on {cfg.collection} and has "
            "not set up 2FA. Log in to the admin once to finish TOTP setup, then re-run."
        )

    client.verify_totp(prompt_totp_code(cfg.totp_code))
    user = client.verify_auth()
    perms = client.collection_permissions(cfg.collection)
    logger.info("Permissions on %s after TOTP: %s", cfg.collection, perms)
    if perms.get("delete") is not True:
        raise SystemExit(
            f"Still no delete permission on {cfg.collection} after TOTP. "
            "Ask a super-admin to grant access."
        )

    if cfg.save_api_key and cfg.env_file:
        key = client.ensure_api_key(user)
        if key:
            upsert_env_var(cfg.env_file, "PAYLOAD_API_KEY", key)
            logger.info(
                "Saved this account's API key to %s as PAYLOAD_API_KEY - "
                "future runs will not ask for a 2FA code",
                cfg.env_file,
            )
    return user


def _finalize_sheet_row(row: SheetRow) -> SheetRow:
    if row.status == "valid" and not row.payload_id and not row.slug:
        row.status = "invalid"
        row.error = "refusing domain-only match"
    row.identity_key = identity_key_for_row(
        row.payload_id, row.domain, row.slug, row.tenant_slug, row.url
    )
    return row


def _row_from_url(
    *,
    row_number: int,
    raw: Mapping[str, str],
    url: str,
    domain_fallback: str = "",
    slug_fallback: str = "",
) -> SheetRow:
    row = SheetRow(row_number=row_number, raw=dict(raw), url=url)
    d, s, err = parse_blog_url(url)
    if err and not (d and s):
        row.status = "invalid"
        row.error = err
    else:
        row.domain = d or (normalize_domain(domain_fallback) if domain_fallback else None)
        row.slug = s or (sanitize_blog_slug(slug_fallback) if slug_fallback else None)
        if row.domain and row.slug:
            row.status = "valid"
        else:
            row.status = "invalid"
            row.error = err or "URL could not yield domain+slug"
    return _finalize_sheet_row(row)


def parse_sheet_rows(
    headers: Sequence[str],
    raw_rows: Sequence[Mapping[str, str]],
    *,
    blog_url_column: Optional[str] = None,
) -> list[SheetRow]:
    id_col = pick_column(headers, ID_COLUMNS)
    url_cols = find_url_columns(headers, explicit=blog_url_column)
    domain_col = pick_column(headers, DOMAIN_COLUMNS)
    slug_col = pick_column(headers, SLUG_COLUMNS)
    tenant_col = pick_column(headers, TENANT_SLUG_COLUMNS)

    if not id_col and not url_cols and not (slug_col and (domain_col or tenant_col)):
        raise ValueError(
            "Sheet must include a Payload ID column, a blog URL column, "
            "or (domain|tenant_slug)+slug columns. "
            f"Headers seen: {list(headers)}"
        )

    out: list[SheetRow] = []
    next_row_number = 1
    for sheet_idx, raw in enumerate(raw_rows, start=1):
        payload_id = (raw.get(id_col) or "").strip() if id_col else ""
        domain = (raw.get(domain_col) or "").strip() if domain_col else ""
        slug = (raw.get(slug_col) or "").strip() if slug_col else ""
        tenant_slug = (raw.get(tenant_col) or "").strip() if tenant_col else ""

        urls_in_row = []
        for uc in url_cols:
            u = (raw.get(uc) or "").strip()
            if u:
                urls_in_row.append(u)

        # Prefer exact Payload IDs; still expand extra URL cells on the same sheet row.
        if payload_id:
            row = SheetRow(row_number=next_row_number, raw=dict(raw), payload_id=payload_id)
            row.status = "valid"
            out.append(_finalize_sheet_row(row))
            next_row_number += 1
            for u in urls_in_row:
                out.append(
                    _row_from_url(
                        row_number=next_row_number,
                        raw=raw,
                        url=u,
                        domain_fallback=domain,
                        slug_fallback=slug,
                    )
                )
                next_row_number += 1
            continue

        if urls_in_row:
            for u in urls_in_row:
                out.append(
                    _row_from_url(
                        row_number=next_row_number,
                        raw={**dict(raw), "_sheet_row": str(sheet_idx)},
                        url=u,
                        domain_fallback=domain,
                        slug_fallback=slug,
                    )
                )
                next_row_number += 1
            continue

        row = SheetRow(row_number=next_row_number, raw=dict(raw))
        next_row_number += 1
        if slug and (domain or tenant_slug):
            row.slug = sanitize_blog_slug(slug)
            row.domain = normalize_domain(domain) if domain else None
            row.tenant_slug = tenant_slug.lower() if tenant_slug else None
            if row.slug and (row.domain or row.tenant_slug):
                row.status = "valid"
            else:
                row.status = "invalid"
                row.error = "missing domain/tenant or slug"
        else:
            row.status = "invalid"
            row.error = "row has no payload id, url, or domain/tenant+slug"
        out.append(_finalize_sheet_row(row))
    return out


def mark_duplicates(rows: Sequence[SheetRow]) -> None:
    seen: dict[str, int] = {}
    for row in rows:
        if row.status != "valid":
            continue
        first = seen.get(row.identity_key)
        if first is None:
            seen[row.identity_key] = row.row_number
        else:
            row.status = "duplicate"
            row.error = f"duplicate of row {first}"


def _tenant_ref(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        return str(value.get("id") or value.get("slug") or value)
    return str(value)


class Matcher:
    def __init__(self, client: PayloadRestClient, collection: str):
        self.client = client
        self.collection = collection
        self._tenant_by_domain: dict[str, Optional[dict[str, Any]]] = {}
        self._tenant_by_slug: dict[str, Optional[dict[str, Any]]] = {}

    def resolve_tenant_domain(self, domain: str) -> Optional[dict[str, Any]]:
        key = normalize_domain(domain)
        if key not in self._tenant_by_domain:
            self._tenant_by_domain[key] = self.client.find_tenant_by_domain(key)
        return self._tenant_by_domain[key]

    def resolve_tenant_slug(self, slug: str) -> Optional[dict[str, Any]]:
        key = slug.strip().lower()
        if key not in self._tenant_by_slug:
            self._tenant_by_slug[key] = self.client.find_tenant_by_slug(key)
        return self._tenant_by_slug[key]

    def match(self, row: SheetRow) -> MatchResult:
        if row.status in ("invalid", "duplicate"):
            return MatchResult(status=row.status, error=row.error)

        if row.payload_id:
            doc = self.client.get_blog_by_id(self.collection, row.payload_id)
            if not doc:
                return MatchResult(status="not_found", error=f"no document id={row.payload_id}")
            return MatchResult(
                status="matched",
                payload_id=str(doc.get("id")),
                slug=doc.get("slug"),
                tenant=_tenant_ref(doc.get("tenant")),
            )

        tenant_id: Optional[str] = None
        tenant_label: Optional[str] = None
        if row.domain:
            tenant = self.resolve_tenant_domain(row.domain)
            if not tenant:
                return MatchResult(status="not_found", error=f"tenant domain not found: {row.domain}")
            tenant_id = str(tenant["id"])
            tenant_label = tenant.get("slug") or row.domain
        elif row.tenant_slug:
            tenant = self.resolve_tenant_slug(row.tenant_slug)
            if not tenant:
                return MatchResult(status="not_found", error=f"tenant slug not found: {row.tenant_slug}")
            tenant_id = str(tenant["id"])
            tenant_label = tenant.get("slug") or row.tenant_slug
        else:
            docs = self.client.find_blogs(self.collection, slug=row.slug, limit=2)
            if not docs:
                return MatchResult(status="not_found", error=f"no blog with slug={row.slug}")
            if len(docs) > 1:
                return MatchResult(
                    status="ambiguous",
                    error=f"slug {row.slug!r} matches multiple tenants; provide domain or payload id",
                    candidates=[str(d.get("id")) for d in docs],
                )
            doc = docs[0]
            return MatchResult(
                status="matched",
                payload_id=str(doc.get("id")),
                slug=doc.get("slug"),
                tenant=_tenant_ref(doc.get("tenant")),
            )

        assert row.slug
        docs = self.client.find_blogs(self.collection, slug=row.slug, tenant_id=tenant_id, limit=2)
        if not docs:
            return MatchResult(
                status="not_found",
                error=f"no blog slug={row.slug} for tenant={tenant_label}",
            )
        if len(docs) > 1:
            return MatchResult(
                status="ambiguous",
                error=f"multiple blogs for slug={row.slug} tenant={tenant_label}",
                candidates=[str(d.get("id")) for d in docs],
            )
        doc = docs[0]
        return MatchResult(
            status="matched",
            payload_id=str(doc.get("id")),
            slug=doc.get("slug"),
            tenant=tenant_label or _tenant_ref(doc.get("tenant")),
        )


class ProgressStore:
    def __init__(self, path: Path):
        self.path = path
        self.completed: dict[str, dict[str, Any]] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.completed = dict(data.get("completed") or {})

    def is_done(self, identity_key: str, *, dry_run: bool = True) -> bool:
        """Skip rows already finished for this mode.

        - deleted / not_found / ambiguous / duplicate / invalid: always skip
        - dry_run: skip only when still in dry-run (so --execute can delete after a dry run)
        """
        entry = self.completed.get(identity_key)
        if not entry:
            return False
        status = entry.get("status")
        if status in ("deleted", "not_found", "ambiguous", "duplicate", "invalid", "skipped"):
            return True
        if status == "dry_run":
            return dry_run
        return False

    def mark(
        self,
        identity_key: str,
        *,
        status: str,
        row_number: int,
        payload_id: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        self.completed[identity_key] = {
            "status": status,
            "row_number": row_number,
            "payload_id": payload_id,
            "error": error,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "completed": self.completed,
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def append_result_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_failed_retry_csv(path: Path, failed_rows: Sequence[SheetRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "row_number",
        "payload_id",
        "url",
        "domain",
        "slug",
        "tenant_slug",
        "matched_id",
        "status",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in failed_rows:
            writer.writerow(
                {
                    "row_number": row.row_number,
                    "payload_id": row.payload_id or "",
                    "url": row.url or "",
                    "domain": row.domain or "",
                    "slug": row.slug or "",
                    "tenant_slug": row.tenant_slug or "",
                    "matched_id": row.matched_id or "",
                    "status": row.status,
                    "error": row.error or "",
                }
            )


def setup_logging(log_file: Path, verbose: bool = True) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def require_confirmation(cfg: RunConfig, report: Mapping[str, Any]) -> None:
    if cfg.dry_run:
        return
    print()
    print("=" * 60)
    print("REAL DELETION PREFLIGHT - review carefully")
    print("=" * 60)
    print(f"  Payload URL:     {cfg.payload_url}")
    print(f"  Collection:      {cfg.collection}")
    print(f"  Sheet rows:      {report.get('total_sheet_rows')}")
    print(f"  Valid rows:      {report.get('valid_rows')}")
    print(f"  Will delete:     {report.get('ready_for_deletion')}")
    print(f"  Not found:       {report.get('not_found')}")
    print(f"  Ambiguous:       {report.get('ambiguous')}")
    print("=" * 60)
    if cfg.yes:
        phrase = os.environ.get("BULK_DELETE_CONFIRM", "")
        if phrase != CONFIRM_PHRASE:
            raise SystemExit(
                f"Refusing real deletion: set BULK_DELETE_CONFIRM={CONFIRM_PHRASE} "
                "together with --yes, or omit --yes and type the phrase interactively."
            )
        print(f"Confirmation received via BULK_DELETE_CONFIRM={CONFIRM_PHRASE}")
        return
    if not sys.stdin.isatty():
        raise SystemExit(
            "Real deletion requires an interactive TTY confirmation, "
            f"or --yes with BULK_DELETE_CONFIRM={CONFIRM_PHRASE}."
        )
    typed = input(f"Type {CONFIRM_PHRASE!r} to proceed with permanent deletion: ").strip()
    if typed != CONFIRM_PHRASE:
        raise SystemExit("Confirmation mismatch - aborting. No records deleted.")


def load_input_csv(cfg: RunConfig) -> tuple[list[str], list[dict[str, str]]]:
    if cfg.csv_path:
        text = Path(cfg.csv_path).read_text(encoding="utf-8-sig")
        return load_csv_text(text)
    if cfg.sheet_csv_url:
        url = google_sheet_export_url(cfg.sheet_csv_url)
        logger.info("Fetching Google Sheet CSV export")
        logger.info("Sheet host: %s", urlparse(url).netloc)
        text = fetch_url_text(url)
        return load_csv_text(text)
    raise ValueError("Provide --csv or --sheet-url / GOOGLE_SHEET_CSV_URL")


def _row_record(row: SheetRow, status: str) -> dict[str, Any]:
    return {
        "row_number": row.row_number,
        "blog_url": row.url,
        "payload_id": row.matched_id or row.payload_id,
        "domain": row.domain,
        "slug": row.slug or row.matched_slug,
        "tenant": row.matched_tenant or row.tenant_slug,
        "status": status,
        "error": row.error,
    }


def _log_row(results_path: Path, row: SheetRow, status: str) -> None:
    rec = _row_record(row, status)
    append_result_jsonl(results_path, rec)
    logger.info(
        "row=%s url=%s id=%s status=%s%s",
        row.row_number,
        row.url or "",
        row.matched_id or row.payload_id or "",
        status,
        f" error={row.error}" if row.error else "",
    )


def print_preflight(report: Mapping[str, Any], cfg: RunConfig) -> None:
    print()
    print("Preflight report")
    print("-" * 40)
    print(f"Payload URL:              {cfg.payload_url}")
    print(f"Collection:               {cfg.collection}")
    print(f"DRY_RUN:                  {cfg.dry_run}")
    print(f"Total sheet rows:         {report['total_sheet_rows']}")
    print(f"Valid rows:               {report['valid_rows']}")
    print(f"Duplicate rows:           {report['duplicate_rows']}")
    print(f"Invalid rows:             {report['invalid_rows']}")
    print(f"Matched Payload records:  {report['matched_payload_records']}")
    print(f"Not found:                {report['not_found']}")
    print(f"Ambiguous:                {report['ambiguous']}")
    print(f"Ready for deletion:       {report['ready_for_deletion']}")
    print(f"Skipped (resume):         {report['skipped_already_done']}")
    print("-" * 40)


def print_final_summary(counters: Counters) -> None:
    print()
    print("Final summary")
    print("-" * 40)
    print(f"Total rows:     {counters.total_rows}")
    print(f"Unique rows:    {counters.unique_rows}")
    print(f"Deleted:        {counters.deleted}")
    print(f"Dry-run:        {counters.dry_run}")
    print(f"Not found:      {counters.not_found}")
    print(f"Ambiguous:      {counters.ambiguous}")
    print(f"Failed:         {counters.failed}")
    print(f"Skipped:        {counters.skipped}")
    print(f"Remaining:      {counters.remaining}")
    print("-" * 40)


def _write_summary(path: Path, cfg: RunConfig, counters: Counters, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": cfg.dry_run,
        "payload_url": cfg.payload_url,
        "collection": cfg.collection,
        "preflight": dict(report),
        "counters": counters.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Wrote summary: %s", path)


def run(cfg: RunConfig) -> Counters:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    progress_path = cfg.progress_file or (cfg.output_dir / "progress.json")
    results_path = cfg.results_file or (cfg.output_dir / f"results-{stamp}.jsonl")
    failed_path = cfg.failed_file or (cfg.output_dir / "failed.jsonl")
    retry_path = cfg.retry_file or (cfg.output_dir / "retry-failed.csv")
    summary_path = cfg.summary_file or (cfg.output_dir / f"summary-{stamp}.json")
    log_path = cfg.log_file or (cfg.output_dir / f"run-{stamp}.log")

    setup_logging(log_path)
    logger.info("Starting bulk-delete (dry_run=%s, collection=%s)", cfg.dry_run, cfg.collection)
    logger.info("Payload URL: %s", cfg.payload_url)
    logger.info("Output dir: %s", cfg.output_dir)

    headers, raw_rows = load_input_csv(cfg)
    if cfg.limit is not None:
        raw_rows = list(raw_rows)[: cfg.limit]

    rows = parse_sheet_rows(headers, raw_rows, blog_url_column=cfg.blog_url_column)
    mark_duplicates(rows)

    counters = Counters(total_rows=len(rows))
    counters.valid_rows = sum(1 for r in rows if r.status == "valid")
    counters.duplicate_rows = sum(1 for r in rows if r.status == "duplicate")
    counters.invalid_rows = sum(1 for r in rows if r.status == "invalid")
    counters.unique_rows = counters.valid_rows

    client = PayloadRestClient(
        cfg.payload_url,
        cfg.api_token,
        auth_mode=cfg.auth_mode,
        max_retries=cfg.max_retries,
        backoff_base_s=cfg.backoff_base_s,
        backoff_max_s=cfg.backoff_max_s,
        request_delay_ms=cfg.request_delay_ms,
    )
    # Stop immediately if credentials are bad or cannot delete — avoid 403 spam on every row.
    establish_session(cfg, client)
    matcher = Matcher(client, cfg.collection)
    progress = ProgressStore(progress_path)

    ready: list[SheetRow] = []
    for row in rows:
        if row.status == "invalid":
            _log_row(results_path, row, "invalid")
            continue
        if row.status == "duplicate":
            counters.skipped += 1
            _log_row(results_path, row, "duplicate")
            progress.mark(
                row.identity_key,
                status="duplicate",
                row_number=row.row_number,
                payload_id=None,
                error=row.error,
            )
            continue
        if progress.is_done(row.identity_key, dry_run=cfg.dry_run):
            prev = progress.completed[row.identity_key]
            row.status = "skipped"
            row.matched_id = prev.get("payload_id")
            row.error = f"already completed as {prev.get('status')}"
            counters.skipped += 1
            _log_row(results_path, row, "skipped")
            continue

        try:
            match = matcher.match(row)
        except PayloadApiError as exc:
            row.status = "failed"
            row.error = str(exc)
            counters.failed += 1
            _log_row(results_path, row, "failed")
            append_result_jsonl(failed_path, _row_record(row, "failed"))
            continue

        if match.status == "matched":
            row.matched_id = match.payload_id
            row.matched_slug = match.slug
            row.matched_tenant = match.tenant
            counters.matched += 1
            ready.append(row)
        elif match.status == "not_found":
            row.status = "not_found"
            row.error = match.error
            counters.not_found += 1
            _log_row(results_path, row, "not_found")
            progress.mark(
                row.identity_key,
                status="not_found",
                row_number=row.row_number,
                payload_id=None,
                error=match.error,
            )
        elif match.status == "ambiguous":
            row.status = "ambiguous"
            row.error = match.error
            counters.ambiguous += 1
            _log_row(results_path, row, "ambiguous")
            progress.mark(
                row.identity_key,
                status="ambiguous",
                row_number=row.row_number,
                payload_id=None,
                error=match.error,
            )
        else:
            row.status = match.status
            row.error = match.error
            _log_row(results_path, row, row.status)

    report = {
        "total_sheet_rows": counters.total_rows,
        "valid_rows": counters.valid_rows,
        "duplicate_rows": counters.duplicate_rows,
        "invalid_rows": counters.invalid_rows,
        "matched_payload_records": len(ready),
        "not_found": counters.not_found,
        "ambiguous": counters.ambiguous,
        "ready_for_deletion": len(ready),
        "skipped_already_done": counters.skipped,
    }
    print_preflight(report, cfg)

    if not ready:
        logger.info("Nothing to delete.")
        counters.remaining = 0
        _write_summary(summary_path, cfg, counters, report)
        return counters

    if not cfg.dry_run:
        require_confirmation(cfg, report)

    failed_rows: list[SheetRow] = []
    for batch_start in range(0, len(ready), cfg.batch_size):
        batch = ready[batch_start : batch_start + cfg.batch_size]
        logger.info(
            "Processing batch %s-%s / %s",
            batch_start + 1,
            batch_start + len(batch),
            len(ready),
        )
        for row in batch:
            assert row.matched_id
            if cfg.dry_run:
                row.status = "dry_run"
                counters.dry_run += 1
                _log_row(results_path, row, "dry_run")
                progress.mark(
                    row.identity_key,
                    status="dry_run",
                    row_number=row.row_number,
                    payload_id=row.matched_id,
                )
                continue
            try:
                client.delete_blog(cfg.collection, row.matched_id)
                row.status = "deleted"
                counters.deleted += 1
                _log_row(results_path, row, "deleted")
                progress.mark(
                    row.identity_key,
                    status="deleted",
                    row_number=row.row_number,
                    payload_id=row.matched_id,
                )
            except PayloadApiError as exc:
                row.status = "failed"
                row.error = str(exc)
                counters.failed += 1
                failed_rows.append(row)
                _log_row(results_path, row, "failed")
                append_result_jsonl(failed_path, _row_record(row, "failed"))
                if exc.status == 404:
                    progress.mark(
                        row.identity_key,
                        status="not_found",
                        row_number=row.row_number,
                        payload_id=row.matched_id,
                        error="404 on delete",
                    )

    if failed_rows:
        write_failed_retry_csv(retry_path, failed_rows)
        logger.info("Wrote retry file: %s (%s rows)", retry_path, len(failed_rows))

    counters.remaining = max(
        0,
        len(ready) - counters.deleted - counters.dry_run - len(failed_rows),
    )
    _write_summary(summary_path, cfg, counters, report)
    print_final_summary(counters)
    return counters


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv_file(path: Path) -> bool:
    """Load KEY=VALUE pairs into os.environ without overriding existing vars.

    Never logs file contents (may contain secrets). Returns True if file was read.
    """
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def load_local_env() -> Optional[Path]:
    """Load the first existing .env from common locations (repo root preferred)."""
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / ".env",
        here.parents[2] / ".env",  # repo root when file is scripts/lib/...
        here.parents[1] / ".env",  # scripts/.env
    ]
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if load_dotenv_file(resolved):
            return resolved
    return None


def _api_key_from_env() -> str:
    raw = os.environ.get("PAYLOAD_API_TOKEN") or os.environ.get("PAYLOAD_API_KEY") or ""
    key = normalize_api_key(raw)
    # Ignore leftover placeholders from .env.example
    if key.lower() in {"", "paste-your-api-key-here", "your-api-key", "xxx"}:
        return ""
    return key


def resolve_auth() -> tuple[str, str, Optional[str], Optional[str]]:
    """Return (api_token, auth_mode, email, password) from the environment.

    No network calls here; `establish_session()` tries the API key first
    (exempt from TOTP) and falls back to email/password + TOTP if needed.
    """
    email = (os.environ.get("PAYLOAD_EMAIL") or os.environ.get("PAYLOAD_USER") or "").strip() or None
    password = os.environ.get("PAYLOAD_PASSWORD") or None
    token = _api_key_from_env()

    if token:
        return token, "api-key", email, password
    if email and password:
        return "", "jwt", email, password

    raise SystemExit(
        "No usable auth in .env.\n"
        "Set PAYLOAD_EMAIL + PAYLOAD_PASSWORD, or PAYLOAD_API_KEY.\n"
        "See .env.example. Do NOT put secrets in .py source.\n"
        "DEPLOY_REPORT_TOKEN cannot delete (read-only CI auth)."
    )


def build_config_from_env_and_args(argv: Optional[Sequence[str]] = None) -> RunConfig:
    loaded = load_local_env()
    if loaded:
        # Path only — never print secret values
        logger.debug("Loaded env file: %s", loaded)
    env_file = loaded or (Path.cwd() / ".env")

    p = argparse.ArgumentParser(
        description="Safe resumable bulk delete of Payload blog-posts listed in a Google Sheet/CSV.",
    )
    p.add_argument("--csv", type=Path, help="Local CSV path (Google Sheet -> File -> Download -> CSV)")
    p.add_argument("--sheet-url", help="Google Sheet CSV export URL or spreadsheet id")
    p.add_argument("--payload-url", default=os.environ.get("PAYLOAD_URL", "").rstrip("/"))
    p.add_argument("--collection", default=os.environ.get("PAYLOAD_COLLECTION", DEFAULT_COLLECTION))
    p.add_argument("--blog-url-column", default=os.environ.get("BLOG_URL_COLUMN"))
    p.add_argument("--limit", type=int, default=None, help="Process only the first N data rows")
    p.add_argument("--batch-size", type=int, default=int(os.environ.get("BATCH_SIZE", "25")))
    p.add_argument("--request-delay-ms", type=int, default=int(os.environ.get("REQUEST_DELAY_MS", "100")))
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("BULK_DELETE_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))),
    )
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    p.add_argument("--execute", dest="execute", action="store_true", help="Disable dry-run (still requires confirmation)")
    p.add_argument("--yes", action="store_true", help=f"Non-interactive; requires BULK_DELETE_CONFIRM={CONFIRM_PHRASE}")
    p.add_argument("--progress-file", type=Path, default=None)
    p.add_argument(
        "--totp-code",
        default=None,
        help="6-digit authenticator code (or PAYLOAD_TOTP_CODE). Prompted interactively if needed.",
    )
    p.add_argument(
        "--no-save-api-key",
        dest="save_api_key",
        action="store_false",
        default=True,
        help="Do not persist the account API key to .env after TOTP login",
    )

    args = p.parse_args(list(argv) if argv is not None else None)

    dry_env = env_bool("DRY_RUN", True)
    if args.execute:
        dry_run = False
    elif args.dry_run is True:
        dry_run = True
    else:
        dry_run = dry_env

    payload_url = (args.payload_url or "").rstrip("/")
    if not payload_url:
        raise SystemExit("PAYLOAD_URL / --payload-url is required")

    sheet_url = args.sheet_url or os.environ.get("GOOGLE_SHEET_CSV_URL")
    csv_path = args.csv
    if not sheet_url and not csv_path:
        raise SystemExit("Provide --csv or --sheet-url / GOOGLE_SHEET_CSV_URL")

    api_token, auth_mode, email, password = resolve_auth()
    return RunConfig(
        payload_url=payload_url,
        api_token=api_token,
        auth_mode=auth_mode,
        email=email,
        password=password,
        totp_code=args.totp_code,
        save_api_key=args.save_api_key,
        env_file=env_file,
        collection=args.collection,
        csv_path=csv_path,
        sheet_csv_url=sheet_url,
        blog_url_column=args.blog_url_column,
        dry_run=dry_run,
        limit=args.limit,
        batch_size=args.batch_size,
        request_delay_ms=args.request_delay_ms,
        output_dir=args.output_dir,
        yes=args.yes,
        progress_file=args.progress_file,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = build_config_from_env_and_args(argv)
    try:
        run(cfg)
    except KeyboardInterrupt:
        logger.error("Interrupted - progress saved; re-run to resume.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
