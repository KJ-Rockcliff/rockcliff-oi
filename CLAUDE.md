# CLAUDE.md — rockcliff-oi

Guidance for Claude Code when working in this repository.

## What this is

**OpenInvoice Bulk PDF Downloader** — an internal Streamlit tool for Rockcliff
Energy Management III Accounting / Audit. It queries receipts from the data
warehouse, lets the user filter them, and bulk-downloads the matching field
tickets and invoices from the Enverus OpenInvoice API as a single ZIP.

The entire application lives in [app.py](app.py) (single file).

## Run it

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

Deployed internally on host `RCEUTIL02`, port 8502 (see the portal repo).

## Architecture

`app.py` is organized into commented sections:

- **Auth helpers** — `_make_mac()` builds an HMAC-SHA256 signature over an empty
  body; `_session()` configures client-certificate (mTLS) auth on a
  `requests.Session`. The OpenInvoice API requires both mTLS and the `mac` header.
- **Database** — `query_receipts()` reads from SQL Server table
  `[bronze_openinvoice].[receipt]` via `pyodbc` using a Trusted Connection.
  Cached with `@st.cache_data(ttl=300)`.
- **Download logic** — `download_receipt()` picks a document source per receipt
  type, with fallbacks:
  - `lem` (field tickets): attachment -> receipt snapshot
  - `general` (invoices): invoice snapshot -> attachment -> receipt snapshot
  - Downloads run concurrently via `ThreadPoolExecutor(max_workers=10)`.
- **Streamlit UI** — sidebar filters (submitted/service date, status, type, plus
  post-query supplier/AFE/cost-center/GL-code filters), preview table, then a
  bulk download that streams results into an in-memory ZIP. Failures are
  collected and offered as a `failures.csv`.

## Secrets and configuration

All secrets are read from `.streamlit/secrets.toml` (gitignored — never commit it):

- `[database]` -> `server`, `database`
- `[openinvoice]` -> `cert_path`, `key_path`, `hmac_key`

The client cert/key (`*.pem`, `*.key`) are gitignored. Do not commit
certificates, keys, or HMAC values to the repo.

## Data source notes

- The `receipt` table uses flattened/denormalized column names with `__`
  separators (e.g. `supplierParty__name`, `lineItems__afe__number`). Line-item
  fields are aggregated with `MAX(...)` and `GROUP BY` to keep one row per receipt.
- Status values in the DB are upper-case: `APPROVED`, `SUBMITTED`, `DISPUTED`,
  `CANCELLED`.

## Conventions

- Python, standard library + `pandas`, `pyodbc`, `requests`, `streamlit`
  (see [requirements.txt](requirements.txt)).
- Keep the single-file structure unless there is a strong reason to split.
- This tool touches financial/audit data — verify query logic and totals against
  the warehouse before relying on output, and confirm changes with the tool owner
  (Kevin Johnson).
