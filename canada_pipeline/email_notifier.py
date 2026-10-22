"""
email_notifier.py
─────────────────
Sends a "Processing Completed" email with the final XML + validation report
bundled as a ZIP attachment.

Uses Microsoft Graph API with MSAL client credentials (app-only flow).
No SMTP password or App Password needed — works with MFA-enforced tenants.

Required Azure App Registration permissions:
    - Mail.Send (Application type, admin-consented)

Environment variables used:
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    EMAIL_FROM  – sender mailbox (must exist in the tenant)
"""

import os
import io
import base64
import zipfile
import logging
from pathlib import Path

import requests
import msal

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


def _get_graph_token() -> str:
    """Acquire an app-only token for Microsoft Graph using MSAL."""
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise RuntimeError("AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET must be set.")

    app = msal.ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
    if result and "access_token" in result:
        return result["access_token"]
    raise RuntimeError(f"Graph token acquisition failed: {result.get('error_description', result)}")


def _build_zip(files: list) -> bytes:
    """Create an in-memory ZIP from a list of file paths."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in files:
            if fpath and os.path.isfile(fpath):
                zf.write(fpath, os.path.basename(fpath))
    return buf.getvalue()


def send_completion_email(
    to_email: str,
    job_id: str,
    final_xml_path: str,
    validation_report_path: str,
    product_type: str = "",
    file_name: str = "",
) -> bool:
    """
    Send a 'Processing Completed' email with the final XML and validation
    report as a ZIP attachment via Microsoft Graph API.

    Args:
        to_email:               Recipient email address.
        job_id:                 Pipeline job ID.
        final_xml_path:         Path to the final .pxml output file.
        validation_report_path: Path to the validation .xlsx report.
        product_type:           Product type (looseleaf/pamphlet).
        file_name:              Original input filename for context.

    Returns:
        True on success, False on failure (logged, never raises).
    """
    if not EMAIL_FROM:
        logger.warning("Email not sent — EMAIL_FROM not configured.")
        return False

    if not to_email:
        logger.warning("Email not sent — no recipient address provided.")
        return False

    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        logger.warning("Email not sent — Azure MSAL credentials not configured.")
        return False

    # ── Collect files to attach ──
    attach_files = []
    if final_xml_path and os.path.isfile(final_xml_path):
        attach_files.append(final_xml_path)
    if validation_report_path and os.path.isfile(validation_report_path):
        attach_files.append(validation_report_path)

    if not attach_files:
        logger.warning("Email not sent — no output files found to attach.")
        return False

    # ── Build ZIP ──
    zip_bytes = _build_zip(attach_files)
    short_name = Path(file_name).stem[:30] if file_name else job_id
    zip_filename = f"ProcessingOutput_{short_name}.zip"

    # ── Build Graph API sendMail payload ──
    body_text = f"""Hi,

Your XML document processing has completed successfully.

Job Details:
  • Job ID      : {job_id}
  • File        : {file_name or 'N/A'}
  • Product     : {product_type or 'N/A'}
  • Attached    : Final XML + Validation Report (ZIP)

Please find the output files attached.

—
XML Track Changes Pipeline (Canada)
"""

    mail_payload = {
        "message": {
            "subject": f"✅ XML Processing Completed — {short_name}",
            "body": {
                "contentType": "Text",
                "content": body_text,
            },
            "toRecipients": [
                {"emailAddress": {"address": to_email}}
            ],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": zip_filename,
                    "contentType": "application/zip",
                    "contentBytes": base64.b64encode(zip_bytes).decode("utf-8"),
                }
            ],
        },
        "saveToSentItems": "false",
    }

    # ── Send via Graph API ──
    try:
        token = _get_graph_token()
        url = f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=mail_payload,
            timeout=30,
        )
        if resp.status_code == 202:
            logger.info("Completion email sent to %s (job=%s) via Graph API", to_email, job_id)
            return True
        else:
            logger.error("Graph sendMail failed (HTTP %s): %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("Failed to send completion email to %s: %s", to_email, e)
        return False
