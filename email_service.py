"""
Brevo (formerly Sendinblue) transactional email integration.
Get a free API key at https://app.brevo.com/settings/keys/api
"""

import json
import os
import urllib.request
import urllib.error

# ── BREVO API CREDENTIALS ──────────────────────────────────────────────────
# Locally: paste your key/sender below, OR set these as environment variables.
# On Render: set BREVO_API_KEY and SENDER_EMAIL in the dashboard's Environment
# tab — never commit a real key into this file if this repo is public.
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "YOUR_BREVO_API_KEY_HERE")

# This must be an email address verified as a sender in your Brevo account
# (Settings → Senders, Domains & Dedicated IPs → Senders)
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "your-verified-sender@example.com")
SENDER_NAME = "ClipForge"
# ───────────────────────────────────────────────────────────────────────────

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def send_otp_email(to_email: str, otp_code: str, purpose: str = "signup") -> tuple:
    """
    Sends an OTP code via Brevo. Returns (success: bool, message: str).
    """
    if BREVO_API_KEY.startswith("YOUR_BREVO"):
        return False, "Brevo API key not configured on the server."

    if purpose == "signup":
        subject = "Verify your ClipForge account"
        heading = "Welcome to ClipForge!"
        body_line = "Use the code below to verify your email and finish creating your account."
    else:
        subject = "Your ClipForge login code"
        heading = "Login verification"
        body_line = "Use the code below to complete your login."

    html_content = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;">
      <h2 style="color:#4b3fb5;">{heading}</h2>
      <p style="color:#333;font-size:15px;">{body_line}</p>
      <div style="background:#f4f2ff;border:1px solid #ddd6ff;border-radius:10px;
                  padding:18px;text-align:center;margin:20px 0;">
        <span style="font-size:32px;font-weight:700;letter-spacing:6px;color:#4b3fb5;">{otp_code}</span>
      </div>
      <p style="color:#888;font-size:13px;">This code expires in 10 minutes. If you didn't request this, you can safely ignore this email.</p>
    </div>
    """

    payload = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_content,
    }

    req = urllib.request.Request(
        BREVO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "application/json",
            "api-key": BREVO_API_KEY,
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                return True, "OTP sent"
            return False, f"Brevo returned status {resp.status}"
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            return False, err_body.get("message", f"Brevo error {e.code}")
        except Exception:
            return False, f"Brevo error {e.code}"
    except urllib.error.URLError as e:
        return False, f"Could not reach Brevo: {e.reason}"
