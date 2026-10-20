"""Streamlit UI for Cloud Bill Spike Explainer's document-ingestion pipeline.

Run locally with OIDC configured in .streamlit/secrets.toml, or explicitly opt into
single-user local demo mode with BILLING_DEMO_MODE=1. Demo mode is not authentication
and must not be enabled for a public deployment.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Iterator

import pandas as pd
import streamlit as st

from billing_explainer import repository as repo
from billing_explainer.config import Settings, load_settings
from billing_explainer.database import connect, init_db
from billing_explainer.ingest import Outcome, ingest_many, read_original_bytes
from billing_explainer.models import DocumentStatus
from billing_explainer.parsers import PARSERS, ParseOptions
from billing_explainer.processing import process_document

st.set_page_config(
    page_title="Cloud Bill Spike Explainer",
    page_icon="☁️",
    layout="wide",
    initial_sidebar_state="expanded",
)

SUPPORTED_TYPES = ["pdf", "csv", "xlsx", "png", "jpg", "jpeg"]


@contextmanager
def open_database(settings: Settings) -> Iterator:
    """Open, initialize, and close a connection for this Streamlit script run."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def _auth_settings_available() -> bool:
    """Check for the minimum Streamlit OIDC configuration without exposing secrets."""
    try:
        auth = st.secrets.get("auth", {})
        required = ("redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url")
        return all(bool(auth.get(key)) for key in required)
    except Exception:
        return False


def require_identity() -> tuple[str, str, str | None, str, bool]:
    """Return (provider, subject, email, display_name, demo_mode).

    Stable OIDC subject is used as the identity key. No identity is accepted from a
    user-editable form field. Local demo mode is only enabled explicitly via an env var.
    """
    demo_mode = os.environ.get("BILLING_DEMO_MODE", "").strip() == "1"
    if demo_mode:
        return "local-demo", "local-demo-user", "demo@example.com", "Local Demo User", True

    if not _auth_settings_available():
        st.title("Cloud Bill Spike Explainer")
        st.error("Authentication is not configured yet.")
        st.markdown(
            "Add an OIDC configuration to `.streamlit/secrets.toml` before using authenticated mode. "
            "For local-only development, you can explicitly enable the single-user demo mode."
        )
        st.code(
            '[auth]\n'
            'redirect_uri = "http://localhost:8501/oauth2callback"\n'
            'cookie_secret = "replace-with-a-long-random-secret"\n'
            'client_id = "YOUR_OIDC_CLIENT_ID"\n'
            'client_secret = "YOUR_OIDC_CLIENT_SECRET"\n'
            'server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"',
            language="toml",
        )
        st.info("Local-only demo: set BILLING_DEMO_MODE=1 in your terminal, then restart Streamlit. Do not enable it on a public deployment.")
        st.stop()

    try:
        if not st.user.is_logged_in:
            st.title("Cloud Bill Spike Explainer")
            st.write("Sign in to upload, process, and review your own billing documents.")
            if st.button("Sign in", type="primary"):
                st.login()
            st.stop()
        subject = str(st.user.get("sub", "")).strip()
        if not subject:
            st.error("The identity provider did not return a stable subject (`sub`). Access is denied.")
            st.stop()
        issuer = str(st.user.get("iss", "")).strip()
        auth_cfg = st.secrets.get("auth", {})
        provider = issuer or str(auth_cfg.get("server_metadata_url", "oidc"))
        provider = f"oidc:{provider}"
        email = st.user.get("email")
        name = st.user.get("name") or email or "Signed-in user"
        return provider, subject, str(email) if email else None, str(name), False
    except Exception as exc:
        st.error(f"Could not complete authentication ({type(exc).__name__}). Check your OIDC configuration.")
        st.stop()


def _money(value: Decimal | None) -> str:
    return "Not detected" if value is None else format(value, "f")


def _doc_label(doc) -> str:
    period = "period not detected"
    if doc.billing_period_start or doc.billing_period_end:
        period = f"{doc.billing_period_start or '?'} → {doc.billing_period_end or '?'}"
    return f"#{doc.id} · {doc.original_filename} · {doc.status.value} · {period}"


def _render_issue_list(notes: list[str]) -> None:
    if not notes:
        st.success("No validation issues were recorded.")
        return
    for note in notes:
        if note.startswith("[failed]") or "[failed]" in note.lower():
            st.error(note)
        elif "[blocking]" in note.lower() or "[review]" in note.lower():
            st.warning(note)
        else:
            st.info(note)


def _render_upload_tab(conn, settings: Settings, user_id: int) -> None:
    st.subheader("Upload billing documents")
    st.write(
        "Upload an invoice and/or detailed billing exports. Each file is stored under your account, "
        "parsed using its format-specific reader, and validated before its extracted records are used."
    )
    st.caption("Supported: PDF, CSV, XLSX, PNG, JPG/JPEG. For files from different billing months, upload them in separate batches if you want to enter different periods.")

    # Keep this checkbox outside the form so changing it immediately reveals the date
    # controls; Streamlit form widgets otherwise do not trigger an independent rerun.
    set_period = st.checkbox("Apply one billing period to all selected files", key="apply_upload_period")
    with st.form("upload_and_process_form", clear_on_submit=True):
        uploaded_files = st.file_uploader(
            "Choose one or more files",
            type=SUPPORTED_TYPES,
            accept_multiple_files=True,
            help=f"Maximum file size: {settings.max_upload_bytes / 1024 / 1024:.0f} MB per file.",
        )
        col_a, col_b = st.columns(2)
        with col_a:
            account_alias = st.text_input("Cloud account / account alias (optional)", max_chars=100)
            currency_hint = st.text_input("Fallback currency (optional)", placeholder="USD", max_chars=3)
        with col_b:
            if set_period:
                today = date.today()
                default_start = today.replace(day=1)
                period_start = st.date_input("Period start", value=default_start, key="upload_period_start")
                period_end = st.date_input("Period end", value=today, key="upload_period_end")
            else:
                period_start = None
                period_end = None
            sheet_name = st.text_input("Excel worksheet name (optional)", placeholder="Leave blank to auto-select")
        submitted = st.form_submit_button("Upload and process", type="primary", use_container_width=True)

    if not submitted:
        return
    if not uploaded_files:
        st.warning("Choose at least one file first.")
        return
    if set_period and period_end < period_start:
        st.error("The billing period end date must not be earlier than the start date.")
        return
    if currency_hint and (len(currency_hint.strip()) != 3 or not currency_hint.strip().isalpha()):
        st.error("Fallback currency must be a three-letter code such as USD, INR, or EUR.")
        return

    files = [(file.name, file.getvalue()) for file in uploaded_files]
    intake_results = ingest_many(
        conn,
        settings,
        user_id,
        files,
        account_alias=account_alias or None,
        billing_period_start=period_start if set_period else None,
        billing_period_end=period_end if set_period else None,
    )

    st.subheader("Processing results")
    for intake in intake_results:
        with st.container(border=True):
            st.markdown(f"**{intake.filename}**")
            if intake.outcome == Outcome.REJECTED:
                st.error(intake.message)
                continue
            if intake.outcome == Outcome.DUPLICATE:
                st.warning(intake.message)
                continue
            doc = intake.document
            if doc is None:
                st.error("Upload was accepted but no document record was returned.")
                continue
            try:
                options = ParseOptions(
                    currency=currency_hint.strip().upper() if currency_hint.strip() else None,
                    sheet_name=sheet_name.strip() or None,
                )
                processed = process_document(conn, settings, user_id, doc.id, options=options)
                latest = processed.document
                st.write(
                    f"Document **#{latest.id}** · status **{latest.status.value}** · "
                    f"line items saved: **{processed.items_saved}**"
                )
                if latest.billing_period_start or latest.billing_period_end:
                    st.caption(f"Billing period: {latest.billing_period_start or '?'} → {latest.billing_period_end or '?'}")
                _render_issue_list(latest.validation_notes)
                if processed.issues:
                    with st.expander("Parser details"):
                        for issue in processed.issues:
                            st.write(str(issue))
            except Exception as exc:
                st.error(f"Processing failed for document #{doc.id}: {type(exc).__name__}: {exc}")
                st.info("The original file remains stored. You can retry parsing from the document review tab.")


def _documents_table(documents) -> pd.DataFrame:
    rows = []
    for doc in documents:
        rows.append({
            "ID": doc.id,
            "Filename": doc.original_filename,
            "Type": doc.file_type.upper(),
            "Status": doc.status.value,
            "Account": doc.account_alias or "—",
            "Provider": doc.cloud_provider or "—",
            "Period start": doc.billing_period_start or "—",
            "Period end": doc.billing_period_end or "—",
            "Uploaded (UTC)": doc.uploaded_at,
            "Size (KB)": round(doc.file_size_bytes / 1024, 1),
            "Reviewed": "Yes" if doc.verified_at else "No",
        })
    return pd.DataFrame(rows)


def _render_history_tab(conn, user_id: int) -> None:
    st.subheader("Your document history")
    docs = repo.list_documents(conn, user_id)
    if not docs:
        st.info("No documents uploaded yet.")
        return
    st.dataframe(_documents_table(docs), use_container_width=True, hide_index=True)
    processed = sum(d.status == DocumentStatus.PROCESSED for d in docs)
    needs_review = sum(d.status == DocumentStatus.NEEDS_REVIEW for d in docs)
    failed = sum(d.status == DocumentStatus.FAILED for d in docs)
    a, b, c, d = st.columns(4)
    a.metric("Total documents", len(docs))
    b.metric("Processed", processed)
    c.metric("Needs review", needs_review)
    d.metric("Failed", failed)


def _render_document_inspector(conn, settings: Settings, user_id: int) -> None:
    st.subheader("Inspect, retry, and verify a document")
    docs = repo.list_documents(conn, user_id)
    if not docs:
        st.info("Upload a document first; it will appear here.")
        return

    labels = {_doc_label(doc): doc.id for doc in docs}
    selected_label = st.selectbox("Select a document", list(labels), key="inspect_document_select")
    document_id = labels[selected_label]
    doc = repo.get_document(conn, user_id, document_id)

    meta_cols = st.columns(4)
    meta_cols[0].metric("Document ID", f"#{doc.id}")
    meta_cols[1].metric("Status", doc.status.value)
    meta_cols[2].metric("Type", doc.file_type.upper())
    meta_cols[3].metric("Size", f"{doc.file_size_bytes / 1024:.1f} KB")
    st.write(f"**Original filename:** {doc.original_filename}")
    st.write(f"**SHA-256:** `{doc.sha256}`")
    st.write(f"**Uploaded at (UTC):** {doc.uploaded_at}")
    st.write(f"**Billing period:** {doc.billing_period_start or 'unknown'} → {doc.billing_period_end or 'unknown'}")
    st.write(f"**Account:** {doc.account_alias or 'not detected'} · **Provider:** {doc.cloud_provider or 'not detected'}")

    try:
        original = read_original_bytes(conn, settings, user_id, doc.id)
        st.download_button(
            "Download original file",
            data=original,
            file_name=doc.original_filename,
            mime={"pdf": "application/pdf", "csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                  "png": "image/png", "jpg": "image/jpeg"}.get(doc.file_type, "application/octet-stream"),
            key=f"download_original_{doc.id}",
        )
    except Exception as exc:
        st.error(f"Original file could not be read: {type(exc).__name__}: {exc}")

    with st.expander("Extracted text / OCR output", expanded=bool(doc.extracted_text)):
        if doc.extracted_text:
            st.text_area("Extracted content", doc.extracted_text, height=260, disabled=True, key=f"extracted_text_{doc.id}")
        else:
            st.caption("No extracted text is available yet. Retry processing below.")

    st.markdown("#### Invoice summary")
    summary = repo.get_invoice_summary(conn, user_id, doc.id)
    if summary is None:
        st.info("No invoice summary was extracted from this file.")
    else:
        summary_data = {
            "Invoice number": summary.invoice_number or "Not detected",
            "Currency": summary.currency or "Not detected",
            "Usage total": _money(summary.usage_total),
            "Credits (negative)": _money(summary.credit_total),
            "Tax total": _money(summary.tax_total),
            "Other adjustments": _money(summary.other_adjustments),
            "Invoice total": _money(summary.invoice_total),
            "Reconciliation status": summary.reconciliation_status.value,
            "Uncertain fields": ", ".join(summary.uncertain_fields) if summary.uncertain_fields else "None recorded",
        }
        st.dataframe(pd.DataFrame([summary_data]), use_container_width=True, hide_index=True)
        if summary.reconciliation_notes:
            st.caption(summary.reconciliation_notes)

    st.markdown("#### Normalized billing line items")
    items = repo.list_line_items(conn, user_id, doc.id)
    if items:
        rows = [{
            "Service": item.service or "Unknown",
            "Region": item.region or "Unknown",
            "Resource ID": item.resource_id or "—",
            "Charge type": item.charge_type.value,
            "Amount": format(item.cost_amount, "f"),
            "Currency": item.currency,
            "Period start": item.period_start or "—",
            "Period end": item.period_end or "—",
            "Source": item.source_locator,
            "Status": item.status.value,
        } for item in items]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        totals: dict[str, Decimal] = {}
        for item in items:
            totals[item.currency] = totals.get(item.currency, Decimal("0")) + item.cost_amount
        st.caption("Line-item totals by currency: " + " · ".join(f"{currency} {format(total, 'f')}" for currency, total in sorted(totals.items())))
    else:
        st.info("No normalized line items were saved. Invoice PDFs and screenshots may need manual review or a detailed CSV/XLSX export.")

    st.markdown("#### Validation findings")
    _render_issue_list(doc.validation_notes)

    with st.expander("Retry parsing with hints"):
        st.write("Use these fields if the parser needs help. Leave them blank to use automatic detection.")
        with st.form(f"retry_parse_form_{doc.id}"):
            hint_cols = st.columns(3)
            with hint_cols[0]:
                retry_currency = st.text_input("Fallback currency", max_chars=3, key=f"retry_currency_{doc.id}")
            with hint_cols[1]:
                retry_sheet = st.text_input("Excel worksheet name", key=f"retry_sheet_{doc.id}")
            with hint_cols[2]:
                retry_cost = st.text_input("Exact cost-column header", key=f"retry_cost_{doc.id}")
            retry = st.form_submit_button("Reprocess document")
        if retry:
            if retry_currency and (len(retry_currency.strip()) != 3 or not retry_currency.strip().isalpha()):
                st.error("Currency must be a three-letter code, for example USD.")
            else:
                overrides = {"cost": retry_cost.strip()} if retry_cost.strip() else {}
                try:
                    outcome = process_document(
                        conn, settings, user_id, doc.id,
                        options=ParseOptions(
                            currency=retry_currency.strip().upper() or None,
                            sheet_name=retry_sheet.strip() or None,
                            column_overrides=overrides,
                        ),
                    )
                    st.success(f"Reprocessing completed: status={outcome.document.status.value}, line items saved={outcome.items_saved}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Reprocessing failed: {type(exc).__name__}: {exc}")

    if doc.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW):
        if doc.verified_at:
            st.success(f"Human review recorded at {doc.verified_at} (UTC).")
        elif st.button("I reviewed these extracted records — mark document verified", type="primary", key=f"verify_{doc.id}"):
            try:
                repo.mark_document_verified(conn, user_id, doc.id)
                st.success("Document marked as reviewed. This records human review; it does not itself prove invoice reconciliation.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not mark document reviewed: {type(exc).__name__}: {exc}")


def main() -> None:
    st.title("☁️ Cloud Bill Spike Explainer")
    st.caption("Stage 3 · Secure document ingestion, extraction, normalization, and persistence")

    provider, subject, email, display_name, demo_mode = require_identity()
    settings = load_settings()
    with open_database(settings) as conn:
        user = repo.get_or_create_user(conn, provider, subject, email)

        with st.sidebar:
            st.markdown("### Signed in")
            st.write(display_name)
            if email:
                st.caption(email)
            st.caption(f"Internal user ID: {user.id}")
            if demo_mode:
                st.warning("Local demo mode is enabled. This is not real authentication.")
            st.divider()
            st.markdown("### Pipeline status")
            st.write("Parsers enabled:")
            for file_type in sorted(PARSERS):
                st.write(f"• {file_type.upper()}")
            st.caption(f"Database: `{settings.db_path}`")
            st.caption(f"Uploads: `{settings.upload_dir}`")
            if not demo_mode and st.button("Log out", use_container_width=True):
                st.logout()
                st.stop()

        upload_tab, history_tab, inspect_tab = st.tabs(["Upload & process", "Document history", "Inspect / review"])
        with upload_tab:
            _render_upload_tab(conn, settings, user.id)
        with history_tab:
            _render_history_tab(conn, user.id)
        with inspect_tab:
            _render_document_inspector(conn, settings, user.id)

        st.divider()
        st.caption(
            "Important: extracted values may need human review. Screenshots and invoice PDFs usually do not contain "
            "enough structured line-item detail to attribute every cost increase; upload a detailed billing export when available."
        )


if __name__ == "__main__":
    main()
