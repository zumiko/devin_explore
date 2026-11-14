"""Automatic customer emails and texts.

Every message is recorded in the case's outbox. Real delivery happens only when a provider is configured AND a demo
recipient is set (DEMO_EMAIL_TO / DEMO_SMS_TO): the demo data's addresses are fake, so real messages are redirected there.
  Email: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM  (works with Microsoft 365 / Outlook or SendGrid)
  Texts: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM
"""
import base64
import json
import os
import smtplib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

SIGN = "\n\nThe AcmePay team"
TEMPLATES = {
    # key: (email subject, email body, text message or None)
    "signup_approved": ("You're verified: welcome to AcmePay",
                        "Hi {first},\n\nYour identity checks passed and your AcmePay account is ready to use." + SIGN,
                        "AcmePay: you're verified and your account is ready. Welcome, {first}!"),
    "signup_review": ("We're reviewing your AcmePay application",
                      "Hi {first},\n\nThanks for signing up. We need a little more time to verify your identity. "
                      "We usually get back to you within 48 hours." + SIGN,
                      None),
    "need_clearer_id": ("Action needed: a clearer photo of your ID",
                        "Hi {first},\n\nWe couldn't read your ID clearly. Please upload a new photo (flat, good light, all four corners "
                        "visible) and a fresh selfie here:\n{link}\n\nThe link works for 7 days.{note}" + SIGN,
                        "AcmePay: we need a clearer photo of your ID to finish opening your account: {link}"),
    "need_selfie_retake": ("Action needed: a new selfie",
                           "Hi {first},\n\nWe couldn't match your selfie to your ID. Please take a new selfie (face the camera, good light, "
                           "no hat or glasses) and upload it with your ID here:\n{link}\n\nThe link works for 7 days.{note}" + SIGN,
                           "AcmePay: please take a new selfie to finish opening your account: {link}"),
    "need_proof_of_address": ("Action needed: proof of address",
                              "Hi {first},\n\nTo finish verifying your account we need proof of your address (a recent utility bill or bank "
                              "statement). Upload it with your ID here:\n{link}\n\nThe link works for 7 days.{note}" + SIGN,
                              "AcmePay: we need proof of address to finish opening your account: {link}"),
    "approved": ("Your AcmePay account is ready",
                 "Hi {first},\n\nGood news: we've verified your identity and your account is now active." + SIGN,
                 "AcmePay: your account is verified and active."),
    "rejected": ("Update on your AcmePay application",
                 "Hi {first},\n\nWe're unable to open an AcmePay account for you at this time. "
                 "If you have questions, reply to this email." + SIGN,
                 None),
    "call_invite": ("Invitation: AcmePay verification call, {when}",
                    "Hi {first},\n\nWe'd like a quick video call to finish verifying your identity.\n\nWhen: {when} ({minutes} min)\n"
                    "Join: {link}\nPlease have your ID with you.{note}\n\nA calendar invite is attached." + SIGN,
                    "AcmePay: verification call {when}. Join: {link}"),
}


def email_provider():
    return "smtp" if os.environ.get("SMTP_HOST") and os.environ.get("DEMO_EMAIL_TO") else None


def sms_provider():
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "DEMO_SMS_TO")
    return "twilio" if all(os.environ.get(k) for k in keys) else None


def _send_email(subject, body, ics):
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["DEMO_EMAIL_TO"]
    msg["Subject"] = subject
    msg.set_content(body)
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="invite.ics",
                           params={"method": "REQUEST"})
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587)), timeout=15) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return None


def _send_sms(text):
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=urllib.parse.urlencode({"To": os.environ["DEMO_SMS_TO"], "From": os.environ["TWILIO_FROM"], "Body": text}).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{os.environ['TWILIO_AUTH_TOKEN']}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["sid"]


def _record(conn, case_id, customer, channel, to_addr, subject, body, provider, sent_by, ics, deliver):
    status, error, provider_id = "outbox only (demo mode)", None, None
    if provider:
        try:
            provider_id = deliver()
            status = "sent"
        except Exception as e:  # noqa: BLE001 - any provider failure is shown on the case, not raised
            status, error = "failed", str(e)[:300]
    cur = conn.execute(
        "INSERT INTO messages (case_id, customer_id, channel, to_addr, subject, body, status, provider, provider_id, error,"
        " ics, sent_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (case_id, customer["id"], channel, to_addr, subject, body, status, provider, provider_id, error, ics, sent_by,
         datetime.now().replace(microsecond=0).isoformat()),
    )
    return cur.lastrowid


def send(conn, case_id, customer, template, sent_by=None, ics=None, **ctx):
    """Send the template's email (and text, if it has one and the customer has a phone). Returns message ids."""
    subject, body, sms = TEMPLATES[template]
    ctx = {"first": customer["name"].split()[0], "note": "", "link": "", **ctx}
    if ctx["note"]:
        ctx["note"] = f"\n\nNote from our team: {ctx['note']}"
    subject, body = subject.format(**ctx), body.format(**ctx)
    ids = [_record(conn, case_id, customer, "email", customer["email"], subject, body, email_provider(), sent_by, ics,
                   lambda: _send_email(subject, body, ics))]
    if sms and customer["phone"]:
        text = sms.format(**ctx)
        ids.append(_record(conn, case_id, customer, "sms", customer["phone"], None, text, sms_provider(), sent_by, None,
                           lambda: _send_sms(text)))
    return ids


def call_invite_ics(start_local, minutes, link, case_id, organizer_email="kyc@acmepay.example"):
    start = start_local.astimezone(timezone.utc)
    end = start + timedelta(minutes=minutes)
    fmt = "%Y%m%dT%H%M%SZ"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AcmePay//Internal Tools//EN", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:kyc-{case_id}-{start.strftime(fmt)}@acmepay.example", f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}", "SUMMARY:AcmePay identity verification call",
        f"LOCATION:{link}", f"DESCRIPTION:Join the video call: {link}", f"ORGANIZER:mailto:{organizer_email}",
        "END:VEVENT", "END:VCALENDAR", "",
    ])
