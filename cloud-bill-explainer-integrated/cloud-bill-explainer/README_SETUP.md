# Cloud Bill Spike Explainer — Stage 3

This stage implements document ingestion and review using Streamlit, SQLite, PyMuPDF, pandas, openpyxl, Pillow, and Tesseract OCR.

## Supported uploads

- PDF (text extraction; OCR fallback for scanned pages; best-effort table detection)
- CSV (normalized cloud billing rows)
- XLSX (best-effort worksheet selection and common table normalization)
- PNG/JPG/JPEG (OCR text extraction; manually review amounts/table rows)

Extracted PDF/image invoice fields are best-effort. Always review uncertain values. A PDF invoice/screenshot often does not contain enough detail for service-by-region cost attribution; detailed CSV/XLSX billing exports are preferred for that task.

## Install

Use Python 3.11 or newer in a virtual environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS/Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Tesseract OCR is a separate system dependency. Install Tesseract and make sure `tesseract` is on PATH. PyMuPDF uses Tesseract for OCR on scanned PDF pages; the image parser calls it via `pytesseract`.

## Local-only demo mode

Demo mode uses one fixed local demo identity and is **not real authentication**. Do not enable it on a public deployment.

Windows PowerShell:

```powershell
$env:BILLING_DEMO_MODE="1"
streamlit run app.py
```

macOS/Linux:

```bash
BILLING_DEMO_MODE=1 streamlit run app.py
```

The default database is `billing.db` and original files are stored under `uploads/`. Both paths can be changed with `BILLING_DB_PATH` and `BILLING_UPLOAD_DIR`; per-file size can be changed using `BILLING_MAX_UPLOAD_MB`.

## OIDC authentication (Google example)

1. Create an OAuth/OIDC client in your identity provider.
2. Register the redirect URL `http://localhost:8501/oauth2callback` for local development.
3. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
4. Fill in `client_id`, `client_secret`, and a long random `cookie_secret`.
5. Leave `BILLING_DEMO_MODE` unset and run `streamlit run app.py`.

The app stores the stable OIDC subject with the issuer/provider identifier, and every document fetch is scoped through the logged-in user's internal database ID. Never commit `.streamlit/secrets.toml`.

## Run tests

```bash
python -m pytest -q
```
