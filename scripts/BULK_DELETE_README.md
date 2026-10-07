# Bulk delete Payload blog posts (safe + resumable)

Standalone utility for the Astro + Payload platform (`astropayload`).  
**Does not modify CMS production code.** Defaults to dry-run.

Inspected against Payload **3.84.1** (`blog-posts` collection).

## Matching fields (from schema — not assumed)

| Priority | Sheet input | Payload match |
|---|---|---|
| 1 | `payload_id` / `id` | `GET/DELETE /api/blog-posts/:id` |
| 2 | Blog URL | Tenant `domain` + post `slug` (last path segment) |
| 3 | `domain`+`slug` or `tenant_slug`+`slug` | `where[and][tenant]+[slug]` |

There is **no** `sourceUrl` on `blog-posts`. Optional `seo.canonicalUrl` is an override only and is **not** used for matching. Never delete by domain alone.

## Auth (why deletes used to 403)

The CMS runs `payload-totp` with `forceSetup: true`. Its access wrapper only lets
**create/update/delete** through for:

- a **User API Key** (`Authorization: users API-Key ...`), or
- an email/password session that has been **TOTP-verified** (`payload-totp` cookie).

`read` is excluded from the wrapper, which is why lookups worked while
`DELETE /api/blog-posts/:id` returned 403 with a plain JWT.

The script handles this automatically:

1. If `PAYLOAD_API_KEY` works, use it (no 2FA, no expiry).
2. Else login with `PAYLOAD_EMAIL`/`PAYLOAD_PASSWORD`, check `GET /api/access`;
   if delete is not allowed, prompt once for the **6-digit authenticator code**
   (`--totp-code` / `PAYLOAD_TOTP_CODE` for non-interactive), `POST /api/verify-totp`.
3. Keep long runs alive via `POST /api/users/refresh-token` (re-issues the TOTP cookie).
4. Save the account's API key to `.env` (`PAYLOAD_API_KEY`) so the next run is unattended.
   An existing key is reused, never rotated. Disable with `--no-save-api-key`.

`DEPLOY_REPORT_TOKEN` is read-only for CMS content and **cannot** delete.

## Secrets (do NOT paste into .py files)

1. Copy `.env.example` to `.env` in the **repo root** (`script/.env`).
2. Set `PAYLOAD_EMAIL` + `PAYLOAD_PASSWORD` (super-admin). `PAYLOAD_API_KEY` is filled in
   automatically after the first 2FA-verified run.
3. `.env` is gitignored.

The script **stops immediately** if it cannot authenticate or cannot delete,
instead of failing every row.

## Commands

```bash
# From this repo root (reads .env automatically)
# Or set env vars in the terminal instead of .env:
export PAYLOAD_URL=https://cms.yourdomain.com
export PAYLOAD_API_KEY='…'   # or PAYLOAD_API_TOKEN

# 10-row dry run (local CSV export from Google Sheets)
python scripts/bulk-delete-payload-blogs.py --csv ./blogs-to-delete.csv --limit 10

# Full dry run
python scripts/bulk-delete-payload-blogs.py --csv ./blogs-to-delete.csv

# Google Sheet (edit URL or CSV export URL both work; gid preserved)
export GOOGLE_SHEET_CSV_URL='https://docs.google.com/spreadsheets/d/1-G5IcB_qMuKsVtqPsedmJVhWdmZKy9NP6ed_LPx3kfs/edit?gid=0#gid=0'
python scripts/bulk-delete-payload-blogs.py --limit 10

# Full dry run against that sheet
python scripts/bulk-delete-payload-blogs.py

# Real deletion (interactive: type DELETE)
python scripts/bulk-delete-payload-blogs.py --execute

# Real deletion non-interactive
BULK_DELETE_CONFIRM=DELETE python scripts/bulk-delete-payload-blogs.py --execute --yes
```

Note: this sheet has **two** `URL` columns side-by-side. The script flattens both into one URL-per-row list (~56k URLs).

## Outputs (`scripts/bulk-delete-output/`)

- `progress.json` — resume checkpoint (saved after every successful delete)
- `results-*.jsonl` — per-row log
- `failed.jsonl` / `retry-failed.csv` — failures for retry
- `summary-*.json` — final counters
- `run-*.log` — run log (tokens never written)

## Tests

```bash
cd scripts
python -m pytest tests/test_bulk_delete_payload_blogs.py -q
```
