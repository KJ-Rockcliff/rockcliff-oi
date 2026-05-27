"""
OpenInvoice Bulk PDF Downloader
Rockcliff Energy Management III — Accounting/Audit
"""

import base64
import hashlib
import hmac as hmac_lib
import io
import json
import os
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import pyodbc
import requests
import streamlit as st

# ── Constants ────────────────────────────────────────────────────────────────
API_BASE = "https://api.openinvoice.com"
SNAP_RECEIPT = API_BASE + "/docp/supply-chain/v1/receipts/{itemID}/snapshot"
SNAP_ATTACH  = API_BASE + "/docp/supply-chain/v1/receipts/{itemID}/attachments/{attachmentId}"
SNAP_INVOICE = API_BASE + "/docp/supply-chain/v1/invoices/{invoiceId}/snapshot"
TIMEOUT_S    = 180
MAX_SUPPLIER_CHARS = 30

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _make_mac(hmac_key: str) -> str:
    """Generate HMAC-SHA256 signature over empty body (GET requests)."""
    raw = hmac_lib.new(hmac_key.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(raw).decode("utf-8")


def _session(cert_path: str, key_path: str) -> requests.Session:
    s = requests.Session()
    s.cert = (cert_path, key_path)
    return s


def _get(session: requests.Session, url: str, hmac_key: str, **kwargs) -> requests.Response:
    headers = {"mac": _make_mac(hmac_key)}
    return session.get(url, headers=headers, timeout=TIMEOUT_S, **kwargs)

# ── Database ──────────────────────────────────────────────────────────────────

def _conn_str() -> str:
    db = st.secrets["database"]
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={db['server']};"
        f"DATABASE={db['database']};"
        "Trusted_Connection=yes;"
    )


@st.cache_data(ttl=300, show_spinner=False)
def query_receipts(
    date_from: str,
    date_to: str,
    statuses: tuple,
    receipt_type: str,
    max_rows: int,
    service_from: str | None = None,
    service_to: str | None = None,
) -> pd.DataFrame:
    type_filter = ""
    if receipt_type == "lem":
        type_filter = "AND r.receiptType = 'lem'"
    elif receipt_type == "general":
        type_filter = "AND r.receiptType = 'general'"

    status_placeholders = ",".join(["?" for _ in statuses])

    service_filter = ""
    if service_from and service_to:
        service_filter = "AND r.serviceDateFrom >= ?\n          AND r.serviceDateFrom <= ?"

    sql = f"""
        SELECT
            r.itemID,
            r.receiptNumber,
            r.status,
            r.submittedDatetime,
            r.totalAmount,
            r.supplierParty__name,
            r.receiptType,
            r.description                            AS well,
            MAX(r.lineItems__afe__number)            AS afe_number,
            MAX(r.lineItems__costCenter__number)     AS cost_center,
            MAX(r.lineItems__major__code)            AS major_code,
            MAX(r.lineItems__major__description)     AS major_desc,
            MAX(r.lineItems__minor__code)            AS minor_code,
            MAX(r.lineItems__minor__description)     AS minor_desc,
            MAX(r.attachments__itemID)               AS attachments__itemID,
            MAX(r.attachments__fileName)             AS attachments__fileName,
            MAX(r.attachments__links)                AS attachments__links,
            MAX(r.referencingInvoices__invoiceID)    AS referencingInvoices__invoiceID
        FROM [bronze_openinvoice].[receipt] r
        WHERE r.submittedDatetime >= ?
          AND r.submittedDatetime <= ?
          AND r.status IN ({status_placeholders})
          {type_filter}
          {service_filter}
        GROUP BY
            r.itemID, r.receiptNumber, r.status,
            r.submittedDatetime, r.totalAmount,
            r.supplierParty__name, r.receiptType, r.description
        ORDER BY r.submittedDatetime DESC
        OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY
    """
    params = [date_from, date_to] + list(statuses)
    if service_from and service_to:
        params += [service_from, service_to]
    params += [max_rows]

    with pyodbc.connect(_conn_str()) as conn:
        return pd.read_sql(sql, conn, params=params)

# ── File-naming ───────────────────────────────────────────────────────────────

def _safe_name(supplier: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", supplier or "")
    return s[:MAX_SUPPLIER_CHARS].strip("_")


def pdf_filename(row: pd.Series) -> str:
    return f"{row['receiptNumber']}_{_safe_name(row['supplierParty__name'])}_{row['itemID']}.pdf"

# ── Download logic ────────────────────────────────────────────────────────────

def _fetch_bytes(session, url, hmac_key) -> bytes:
    """GET with one retry on timeout."""
    try:
        r = _get(session, url, hmac_key)
    except requests.Timeout:
        r = _get(session, url, hmac_key)   # single retry
    r.raise_for_status()
    if "application/pdf" not in r.headers.get("Content-Type", ""):
        raise ValueError(f"Response is not PDF (Content-Type: {r.headers.get('Content-Type')})")
    return r.content


def _attachment_url(row: pd.Series) -> str | None:
    """Parse href from attachments__links JSON and return full URL, or None."""
    links_raw = row.get("attachments__links")
    attach_id = row.get("attachments__itemID")
    if pd.isna(links_raw) or pd.isna(attach_id):
        return None
    try:
        links = json.loads(links_raw)
        for link in links:
            if link.get("rel") == "self":
                return API_BASE + link["href"]
    except Exception:
        pass
    # Fallback: construct URL from known IDs
    return SNAP_ATTACH.format(itemID=int(row["itemID"]), attachmentId=int(attach_id))


def download_receipt(row: pd.Series, session: requests.Session, hmac_key: str) -> tuple[bytes, str]:
    """
    Returns (pdf_bytes, filename) or raises.
    Strategy:
      LEM  → 1) attachment  2) receipt snapshot
      general → 1) invoice snapshot  2) attachment
    """
    fname = pdf_filename(row)
    item_id = int(row["itemID"])
    receipt_type = (row.get("receiptType") or "").lower()

    if receipt_type == "lem":
        # Preferred: supplier-uploaded attachment
        attach_url = _attachment_url(row)
        if attach_url:
            try:
                data = _fetch_bytes(session, attach_url, hmac_key)
                attach_fname = row.get("attachments__fileName") or fname
                return data, attach_fname
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    pass   # fall through to snapshot
                else:
                    raise
        # Fallback: receipt snapshot
        url = SNAP_RECEIPT.format(itemID=item_id)
        data = _fetch_bytes(session, url, hmac_key)
        snap_fname = f"{row['receiptNumber']}_snapshot.pdf"
        return data, snap_fname

    else:  # general / unknown
        # Preferred: invoice snapshot
        inv_id = row.get("referencingInvoices__invoiceID")
        if not pd.isna(inv_id):
            try:
                url = SNAP_INVOICE.format(invoiceId=int(inv_id))
                data = _fetch_bytes(session, url, hmac_key)
                inv_fname = f"{row['receiptNumber']}_invoice.pdf"
                return data, inv_fname
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    pass   # fall through to attachment
                else:
                    raise
        # Fallback: attachment
        attach_url = _attachment_url(row)
        if attach_url:
            data = _fetch_bytes(session, attach_url, hmac_key)
            attach_fname = row.get("attachments__fileName") or fname
            return data, attach_fname
        # Last resort: receipt snapshot
        url = SNAP_RECEIPT.format(itemID=item_id)
        data = _fetch_bytes(session, url, hmac_key)
        snap_fname = f"{row['receiptNumber']}_snapshot.pdf"
        return data, snap_fname


def _classify_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code}: Authentication failed — verify cert, key, and HMAC key in secrets.toml"
        if code == 404:
            return f"HTTP 404: Document not found (all fallbacks exhausted)"
        return f"HTTP {code}: {msg}"
    if "SSL" in msg or "certificate" in msg.lower() or "handshake" in msg.lower():
        return f"mTLS failed — confirm cert/key are PEM format and cert is registered with Enverus: {msg}"
    if isinstance(exc, requests.Timeout):
        return "Timeout after retry"
    return msg

# ── Streamlit UI ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="OpenInvoice PDF Downloader",
    page_icon="📄",
    layout="wide",
)

st.title("OpenInvoice Bulk PDF Downloader")
st.caption("Rockcliff Energy Management III — Accounting / Audit")

# ── Sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")

    today = date.today()

    st.markdown("**Submitted Date**")
    col1, col2 = st.columns(2)
    with col1:
        date_from = st.date_input("From", value=today - timedelta(days=90), key="sub_from")
    with col2:
        date_to = st.date_input("To", value=today, key="sub_to")

    st.markdown("**Service Date**")
    filter_service = st.checkbox("Filter by service date", value=False)
    col3, col4 = st.columns(2)
    with col3:
        service_from = st.date_input("From", value=today - timedelta(days=90), key="svc_from", disabled=not filter_service)
    with col4:
        service_to = st.date_input("To", value=today, key="svc_to", disabled=not filter_service)

    status_options = ["APPROVED", "PENDING", "REJECTED", "VOIDED"]
    statuses = st.multiselect("Status", status_options, default=["APPROVED"])

    receipt_type_label = st.selectbox(
        "Receipt Type",
        ["All", "Field Tickets only (LEM)", "Invoices only (General)"],
    )
    receipt_type_map = {
        "All": "all",
        "Field Tickets only (LEM)": "lem",
        "Invoices only (General)": "general",
    }
    receipt_type = receipt_type_map[receipt_type_label]

    max_records = st.number_input("Max records", min_value=1, max_value=5000, value=500, step=50)

    st.divider()
    preview_btn = st.button("Preview", type="primary", use_container_width=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "df" not in st.session_state:
    st.session_state.df = None
if "zip_bytes" not in st.session_state:
    st.session_state.zip_bytes = None
if "failures" not in st.session_state:
    st.session_state.failures = []

# ── Preview ───────────────────────────────────────────────────────────────────
if preview_btn:
    if not statuses:
        st.warning("Select at least one status.")
    else:
        with st.spinner("Querying database..."):
            try:
                df = query_receipts(
                    date_from=date_from.isoformat(),
                    date_to=(date_to + timedelta(days=1)).isoformat(),
                    statuses=tuple(statuses),
                    receipt_type=receipt_type,
                    max_rows=max_records,
                    service_from=service_from.isoformat() if filter_service else None,
                    service_to=(service_to + timedelta(days=1)).isoformat() if filter_service else None,
                )
                st.session_state.df = df
                st.session_state.zip_bytes = None
                st.session_state.failures = []
            except Exception as e:
                st.error(f"Database error: {e}")

df: pd.DataFrame | None = st.session_state.df

if df is not None:
    if df.empty:
        st.info("No records match the selected filters.")
    else:
        # Post-query filters (avoids extra DB round-trip)
        suppliers   = sorted(df["supplierParty__name"].dropna().unique())
        all_afes    = sorted(df["afe_number"].dropna().unique().tolist())
        all_ccs     = sorted(df["cost_center"].dropna().unique().tolist())
        all_majors  = sorted(df["major_code"].dropna().unique().tolist())
        all_minors  = sorted(df["minor_code"].dropna().unique().tolist())
        with st.sidebar:
            selected_supplier = st.selectbox(
                "Supplier",
                ["All suppliers"] + suppliers,
                key="supplier_filter",
            )
            selected_afe = st.selectbox(
                "AFE",
                ["All AFEs"] + all_afes,
                key="afe_filter",
            )
            selected_cc = st.selectbox(
                "Cost Center",
                ["All cost centers"] + all_ccs,
                key="cc_filter",
            )
            selected_major = st.selectbox(
                "Major Code",
                ["All major codes"] + all_majors,
                key="major_filter",
            )
            selected_minor = st.selectbox(
                "Minor Code",
                ["All minor codes"] + all_minors,
                key="minor_filter",
            )

        if selected_supplier != "All suppliers":
            df = df[df["supplierParty__name"] == selected_supplier]
        if selected_afe != "All AFEs":
            df = df[df["afe_number"] == selected_afe]
        if selected_cc != "All cost centers":
            df = df[df["cost_center"] == selected_cc]
        if selected_major != "All major codes":
            df = df[df["major_code"] == selected_major]
        if selected_minor != "All minor codes":
            df = df[df["minor_code"] == selected_minor]

        total_amount = float(df["totalAmount"].astype(float).sum())
        st.markdown(f"**{len(df):,} unique receipts &nbsp;|&nbsp; ${total_amount:,.2f} total**")

        df["major"] = df["major_code"].fillna("") + " - " + df["major_desc"].fillna("")
        df["minor"] = df["minor_code"].fillna("") + " - " + df["minor_desc"].fillna("")

        st.dataframe(
            df[[
                "receiptNumber", "well", "afe_number", "cost_center",
                "major", "minor",
                "supplierParty__name", "receiptType",
                "submittedDatetime", "totalAmount", "status",
            ]].rename(columns={
                "receiptNumber":       "Receipt #",
                "well":                "Well",
                "afe_number":          "AFE",
                "cost_center":         "Cost Center",
                "major":               "Major",
                "minor":               "Minor",
                "supplierParty__name": "Supplier",
                "receiptType":         "Type",
                "submittedDatetime":   "Submitted",
                "totalAmount":         "Amount",
                "status":              "Status",
            }),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Download PDFs ─────────────────────────────────────────────────────
        download_btn = st.button("Download PDFs", type="primary")

        if download_btn:
            # Validate secrets
            try:
                oi_secrets = st.secrets["openinvoice"]
                cert_path = oi_secrets["cert_path"]
                key_path  = oi_secrets["key_path"]
                hmac_key  = oi_secrets["hmac_key"]
            except KeyError as e:
                st.error(f"Missing secret: {e}. Check .streamlit/secrets.toml.")
                st.stop()

            if not os.path.isfile(cert_path):
                st.error(f"Certificate file not found: {cert_path}")
                st.stop()
            if not os.path.isfile(key_path):
                st.error(f"Private key file not found: {key_path}")
                st.stop()

            records = df.to_dict("records")
            total   = len(records)
            done    = 0
            failed  = 0
            failures: list[dict] = []
            pdf_files: dict[str, bytes] = {}   # filename → bytes

            progress   = st.progress(0.0, text="Starting…")
            status_box = st.empty()
            log_lines: list[str] = []

            def _log(msg: str):
                log_lines.append(msg)
                # Keep last 30 lines visible
                status_box.code("\n".join(log_lines[-30:]), language=None)

            session = _session(cert_path, key_path)

            def _worker(row_dict):
                row = pd.Series(row_dict)
                try:
                    data, fname = download_receipt(row, session, hmac_key)
                    # Dedupe filename collisions
                    base, ext = os.path.splitext(fname)
                    unique = fname
                    i = 1
                    while unique in pdf_files:
                        unique = f"{base}_{i}{ext}"
                        i += 1
                    return ("ok", fname, unique, data, row)
                except Exception as exc:
                    return ("fail", pdf_filename(row), None, None, row, _classify_error(exc))

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_worker, r): r for r in records}
                for fut in as_completed(futures):
                    result = fut.result()
                    if result[0] == "ok":
                        _, orig_fname, unique_fname, data, row = result
                        pdf_files[unique_fname] = data
                        done += 1
                        _log(f"✓ {row['receiptNumber']} — {row['supplierParty__name']} → {unique_fname}")
                    else:
                        _, orig_fname, _, _, row, err_msg = result
                        failed += 1
                        failures.append({
                            "receiptNumber":      row["receiptNumber"],
                            "itemID":             row["itemID"],
                            "supplierParty__name": row["supplierParty__name"],
                            "error_message":      err_msg,
                        })
                        _log(f"✗ {row['receiptNumber']} — {row['supplierParty__name']}: {err_msg}")

                    pct = (done + failed) / total
                    progress.progress(pct, text=f"{done + failed} of {total} processed | {done} downloaded | {failed} failed")

            progress.progress(1.0, text=f"Complete — {done} downloaded, {failed} failed")

            # Build ZIP in memory
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname, data in pdf_files.items():
                    zf.writestr(fname, data)
            zip_buf.seek(0)

            st.session_state.zip_bytes  = zip_buf.read()
            st.session_state.failures   = failures

        # ── Result downloads ──────────────────────────────────────────────────
        if st.session_state.zip_bytes:
            st.success("PDFs are ready.")
            st.download_button(
                label="⬇  Download ZIP",
                data=st.session_state.zip_bytes,
                file_name=f"openinvoice_pdfs_{date.today().isoformat()}.zip",
                mime="application/zip",
                type="primary",
            )

        if st.session_state.failures:
            fail_df = pd.DataFrame(st.session_state.failures)
            csv_bytes = fail_df.to_csv(index=False).encode()
            st.warning(f"{len(st.session_state.failures)} receipt(s) failed to download.")
            st.download_button(
                label="⬇  Download failures.csv",
                data=csv_bytes,
                file_name=f"openinvoice_failures_{date.today().isoformat()}.csv",
                mime="text/csv",
            )
