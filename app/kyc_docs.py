"""Render fake KYC documents (a specimen ID card and a selfie) from a sample face image."""
import io
import os
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

FACES_DIR = os.path.join(os.path.dirname(__file__), "sample_faces")


def _font(size, bold=False):
    for name in (("DejaVuSans-Bold.ttf", "Arial Bold.ttf") if bold else ("DejaVuSans.ttf", "Arial.ttf")):
        for d in ("", "/usr/share/fonts/truetype/dejavu/", "/Library/Fonts/", "/System/Library/Fonts/Supplemental/"):
            try:
                return ImageFont.truetype(d + name, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def id_card(face_file, name, dob, doc_number, state, expires="2031-12-31"):
    """A US-style driver's license marked SPECIMEN, with the customer's own details."""
    w, h = 1000, 630
    card = Image.new("RGB", (w, h), (232, 238, 245))
    d = ImageDraw.Draw(card)
    for y in range(0, h, 14):
        d.line([(0, y), (w, y + 60)], fill=(222, 230, 240))
    d.rectangle([0, 0, w, 80], fill=(30, 58, 110))
    d.text((30, 18), f"STATE OF {state}  ·  DRIVER LICENSE", fill="white", font=_font(34, True))
    photo = Image.open(os.path.join(FACES_DIR, face_file)).convert("RGB").resize((280, 350))
    photo = ImageEnhance.Color(photo).enhance(0.75)
    card.paste(photo, (30, 110))
    d.rectangle([30, 110, 310, 460], outline=(70, 70, 70), width=2)
    parts = name.split()
    rows = [("DL No.", doc_number), ("Last name", parts[-1].upper()), ("First name", " ".join(parts[:-1]).upper() or "-"),
            ("Date of birth", dob),
            ("Expires", expires), ("Class", "D")]
    x, y = 350, 110
    for k, v in rows:
        d.text((x, y), k, fill=(95, 95, 95), font=_font(18))
        d.text((x, y + 20), v, fill=(20, 20, 20), font=_font(28, True))
        y += 58
    d.text((620, 500), "SPECIMEN", fill=(200, 70, 70), font=_font(52, True))
    d.text((30, 500), "Demo data. Not a real document.", fill=(110, 110, 110), font=_font(18))
    return _jpeg(card)


def selfie(face_file, seed):
    """A phone-selfie-looking variant of the sample face: mirrored, tilted, recropped, different light."""
    rng = random.Random(seed)
    img = ImageOps.mirror(Image.open(os.path.join(FACES_DIR, face_file)).convert("RGB"))
    img = img.rotate(rng.uniform(-7, 7), resample=Image.BICUBIC, expand=False, fillcolor=(60, 60, 60))
    w, h = img.size
    m = int(w * rng.uniform(0.03, 0.08))
    img = img.crop((m, m, w - m, h - m)).resize((480, 600))
    img = ImageEnhance.Brightness(img).enhance(rng.uniform(0.85, 1.15))
    img = ImageEnhance.Color(img).enhance(rng.uniform(0.9, 1.2))
    r, g, b = img.split()
    img = Image.merge("RGB", (r.point(lambda v: min(255, int(v * 1.06))), g, b.point(lambda v: int(v * 0.94))))
    return _jpeg(img.filter(ImageFilter.GaussianBlur(0.6)))
