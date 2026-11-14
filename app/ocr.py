"""Read key fields off an ID photo with Tesseract OCR (open source) and compare them with what the customer typed.

Demo-grade: tuned for clear, front-on photos of a US-style card. A real vendor (or AI Builder's ID reader)
also handles passports' machine-readable zone, glare, angles and hundreds of document layouts.
"""
import difflib
import io
import re
from datetime import date

from PIL import Image, ImageOps

try:
    import pytesseract
except ImportError:
    pytesseract = None

LABELS = {
    "doc_number": r"(dl|id|document|passport)\s*(no|number|#)",
    "last_name": r"(last\s*name|surname)",
    "first_name": r"(first\s*name|given\s*names?)",
    "dob": r"(date\s*of\s*birth|dob|birth)",
    "expires": r"(expires|expiry|exp)\b",
}
DATE = r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})"


class OcrError(Exception):
    pass


def _norm_date(s):
    if not s:
        return None
    m = re.search(DATE, s)
    if not m:
        return None
    v = m.group(1)
    if "/" in v:
        mm, dd, yyyy = v.split("/")
        v = f"{yyyy}-{mm}-{dd}"
    return v


def read_id(image_bytes):
    if pytesseract is None:
        raise OcrError("OCR isn't installed (pip install pytesseract, plus the tesseract program).")
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("L")
    except Exception:
        raise OcrError("Couldn't open the ID image.")
    if img.width < 1400:
        img = img.resize((1400, int(img.height * 1400 / img.width)))
    try:
        text = pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError:
        raise OcrError("OCR isn't installed (the tesseract program is missing).")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    fields = {}
    for i, line in enumerate(lines):
        for key, pattern in LABELS.items():
            if key in fields or not re.search(pattern, line, re.I):
                continue
            after = re.split(pattern, line, maxsplit=1, flags=re.I)[-1].strip(" :.-")
            value = after if len(after) > 1 else (lines[i + 1] if i + 1 < len(lines) else "")
            fields[key] = value.strip()
            break
    for key in ("dob", "expires"):
        fields[key] = _norm_date(fields.get(key))
    name = " ".join(x for x in (fields.pop("first_name", ""), fields.pop("last_name", "")) if x).title()
    fields["name"] = name or None
    if fields.get("doc_number"):
        n = re.sub(r"[^A-Z0-9]", "", fields["doc_number"].upper())
        fields["doc_number"] = (n[0] + n[1:].replace("O", "0")) if n else None
    return {"fields": fields, "raw_text": text}


def compare(read, customer, name_match_pct=95):
    """Rows of (field, on ID, customer entered, ok) plus a list of problems."""
    f = read["fields"]
    rows, problems = [], []
    name_pct = round(difflib.SequenceMatcher(None, (f.get("name") or "").lower(), customer["name"].lower()).ratio() * 100)
    name_ok = name_pct >= name_match_pct
    rows.append({"field": "Name", "on_id": f.get("name"), "entered": customer["name"], "ok": name_ok,
                 "note": f"{name_pct}% similar"})
    if not name_ok:
        problems.append(f"Name on ID '{f.get('name') or 'unreadable'}' vs '{customer['name']}' ({name_pct}% similar)")
    dob_ok = f.get("dob") == customer["dob"]
    rows.append({"field": "Date of birth", "on_id": f.get("dob"), "entered": customer["dob"], "ok": dob_ok, "note": ""})
    if not dob_ok:
        problems.append(f"Date of birth on ID '{f.get('dob') or 'unreadable'}' vs '{customer['dob']}'")
    expired = bool(f.get("expires")) and f["expires"] < date.today().isoformat()
    rows.append({"field": "Expires", "on_id": f.get("expires"), "entered": None, "ok": bool(f.get("expires")) and not expired,
                 "note": "expired" if expired else ("" if f.get("expires") else "couldn't read")})
    rows.append({"field": "ID number", "on_id": f.get("doc_number"), "entered": None, "ok": bool(f.get("doc_number")), "note": ""})
    return {"rows": rows, "problems": problems, "expired": expired}


def check(image_bytes, customer, name_match_pct=95):
    """Read the ID and compare with the customer's details; never raises, so a bad photo can't block signup."""
    try:
        read = read_id(image_bytes)
    except OcrError as e:
        return {"error": str(e), "rows": [], "problems": [], "expired": False}
    result = compare(read, customer, name_match_pct)
    result["fields"] = read["fields"]
    if not any(read["fields"].values()):
        result["problems"] = ["Couldn't read any details from the ID"]
    return result
