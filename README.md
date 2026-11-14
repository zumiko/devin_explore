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
- **Face match (KYC case page):** upload a fake passport/ID photo and a selfie; an open-source model (OpenCV YuNet face detector + SFace face recognizer, code in `app/face_match.py`) finds both faces and scores how alike they are (0-100). The score maps to "likely same person / reviewer decides / likely different people" using thresholds on the Rules page, and each run is saved and audit-logged. This stands in for the face-match step a KYC vendor would run; it has no liveness or document-authenticity checks. Model files (~40 MB) download into `app/models/` on first use.
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
