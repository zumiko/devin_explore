# devin_explore

Take-home exploring whether a fintech team can use Devin to build internal tools instead of paying for Microsoft Power Apps.

- Assignment brief: [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md)

## Prototype: AcmePay Internal Tools (first draft)

A single Flask app with plain server-rendered pages, running on one SQLite database. All three of the client's Power Apps apps run on top of the same shared building blocks:

| Building block | Where you see it |
|---|---|
| Role-based access | "Acting as" switcher (top right); each role sees different pages and actions |
| Audit trail | Every change goes to `audit_log`, shown per record ("History") and on the Audit Log page |
| Approvals | Shared `approvals` table; refunds are routed by amount |
| Business rules as data | `settings` table (Rules page): refund thresholds, KYC second-review score |
| Shared data | One `customers` table used by KYC, refunds, and flag targeting |

**Apps**

- **KYC review queue:** case list sorted by risk and deadline; a case page with customer details, MockVerify flags and a side-by-side watchlist comparison. Claim, then approve / reject / request info / escalate. A reason and note are required. High-risk cases need a second, different senior reviewer.
- **Face match (KYC case page):** the customer's ID photo and selfie are compared at signup by an open-source model (OpenCV YuNet face detector + SFace face recognizer, `app/face_match.py`), standing in for the KYC vendor. The case page shows both documents, the faces found and a 0-100 score mapped to "likely same person / reviewer decides / likely different people" (thresholds on the Rules page). Reviewers can upload new photos (e.g. after asking for a retake) to re-run it; every run is saved and audit-logged. No liveness or document-authenticity checks. Seven open cases (#1, #2, #3 and four more) have sample documents generated from public-domain AI faces (`app/sample_faces/`, see CREDITS.md); case #2's selfie is a different person. Model files (~40 MB) download into `app/models/` on first run.
- **Customer signup page (`/signup`, "Customer signup" in the top bar):** a customer-facing account form (details + ID photo + selfie). It runs the face match and a fuzzy name check against the watchlist. If everything passes, the customer sees "You're verified" and the case is recorded as auto-approved; otherwise they see "We'll be in touch soon" (never the reason) and a new case lands in the KYC queue with the photos and score attached. Cutoffs are on the Rules page (`face_match_pass_score`, `watchlist_review_pct`).
- **OCR on the ID (`app/ocr.py`):** Tesseract (open source) reads name, date of birth, expiry and ID number off the ID photo; the case page compares them with what the customer typed. A mismatch or an expired ID sends a signup to review (case #10 has an expired ID). Needs the `tesseract` program (`brew install tesseract` / `apt install tesseract-ocr`); without it the app still runs and says OCR is unavailable.
- **Customer emails, texts and call invites (`app/notify.py`):** sent automatically at signup, when a reviewer asks for more info (with a one-time link where the customer uploads new photos, which re-runs the face match and OCR and puts the case back in review), and on approval/rejection. "Invite to a video call" sends a calendar invite (.ics) with a Jitsi link. Every message shows in the case's "Customer contact" outbox. Real delivery needs SMTP (e.g. Microsoft 365) and/or Twilio settings plus `DEMO_EMAIL_TO` / `DEMO_SMS_TO`: since the demo customers' addresses are fake, real messages are always redirected to those. Set `PUBLIC_BASE_URL` so links in messages point at the right host.
- **KYC dashboard (`/kyc/dashboard`, compliance lead and admin only):** 12 weeks of signups by outcome (auto-approved / approved by reviewer / rejected / open), typical hours to a decision and % past the deadline per week, and a per-reviewer table (cases decided, approval rate, typical time, within deadline, open now, weekly sparkline). The seed adds ~380 historical signups for this; five weeks ago Priya was on leave and Marcus's turnaround and deadline misses spike.
- **Ask the data (`/ask`, `app/ask.py`):** type a question in plain English; an AI model (set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, optional `ASK_MODEL`) writes one SQLite SELECT, shown on screen and editable. The query runs on a read-only connection with a SQLite authorizer: only tables the person's role may see, personal data columns (email, phone, address, DOB, SSN) always come back blank, no writes/PRAGMA/ATTACH/extensions, a time budget and a 200-row cap. Every query is written to the audit log. Without an AI key you can still type SQL to see the checks.
- **Users & access (`/admin/access`, admin only):** every person's role and the Entra group that grants it, a dropdown to change someone's role (demo mode; with Microsoft sign-in the group in Entra decides), and a grid of roles vs permissions stored in the `role_permissions` table (defaults in `app/access.py`). Pages check these on the server, and every change goes to the audit log. Admin (IT) can view KYC cases and refunds but can't decide cases or approve refunds; compliance edits the KYC rules and finance edits the refund limits.
- **Sign in with Microsoft (`app/entra.py`, off until configured):** set `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET` and `SECRET_KEY` and the "Acting as" switcher disappears: every staff page requires signing in on Microsoft's page (MFA and device rules stay in Entra), the role comes from membership in Entra security groups (`AcmePay-KYC-Analyst`, `AcmePay-Admin`, ...), people in no AcmePay group are refused, and sign-ins/refusals go to the audit log. Delegated Graph permissions needed: `User.Read`, `GroupMember.Read.All`; redirect URI `<PUBLIC_BASE_URL>/auth/callback`. `python app/entra_provision.py` creates the role groups and one Entra account per seeded staff member in a test tenant (application permissions `User.ReadWrite.All`, `Group.ReadWrite.All`); generated passwords go to `app/entra_test_users.local.json` (git-ignored). Always on, demo or not: CSRF tokens on every staff form, HttpOnly/SameSite session cookies (Secure when `PUBLIC_BASE_URL` is https), 8-hour sessions.
- **Refunds:** start from a customer's transaction. Up to $50 is approved automatically, up to $500 needs a support lead, and above that a lead and then finance. You can't approve your own request. The dashboard shows weekly refunds by reason, by agent, repeat refunders and recent flag changes.
- **Feature flags:** turn flags on or off and set the rollout %. Critical flags can only be changed by engineering, and require a reason.

**Planted demo stories** (all data is fake, generated with a fixed seed in `app/seed.py`)

- KYC #1 John Smith: a false-positive sanctions match (different DOB and country).
- KYC #2 Alex Mercer: a strong match plus a selfie mismatch.
- KYC #3 Grace Holloway: high risk, so Priya's approval is routed to a second reviewer.
- Refunds: a $750 refund waiting for finance, a $1,200 refund waiting for a lead, a repeat refunder, and an agent (Tom) with outsized volume.
- `new_fee_engine` was switched on 3 weeks ago, and "fee error" refunds spiked right after (Refund Dashboard).

## Run it

```bash
pip install -r requirements.txt
python app/seed.py    # optional; the app seeds itself on first run
python app/app.py     # http://localhost:5000
```

"Reset demo data" on the home page regenerates the database.
