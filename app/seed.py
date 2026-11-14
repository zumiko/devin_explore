"""Generate deterministic fake data for the internal tools demo.

Everything here is fictional: people, the "MockVerify" vendor, and the watchlist.
Run directly (`python app/seed.py`) or via the Reset button in the app.
"""
import json
import os
import random
from datetime import datetime, timedelta

from faker import Faker

import face_match
import kyc_docs
import ocr
from db import DB_PATH, audit, connect

SEED = 42
FEE_ENGINE_DAYS_AGO = 21

USERS = [
    (1, "Priya Shah", "kyc_analyst", "Compliance"),
    (2, "Marcus Lee", "kyc_senior", "Compliance"),
    (3, "Dana Brooks", "compliance_lead", "Compliance"),
    (4, "Jake Turner", "support_agent", "Support"),
    (5, "Tom Reyes", "support_agent", "Support"),
    (6, "Aisha Khan", "support_agent", "Support"),
    (7, "Sofia Alvarez", "support_lead", "Support"),
    (8, "Ken Watanabe", "finance", "Finance"),
    (9, "Lena Fischer", "pm", "Product"),
    (10, "Omar Haddad", "engineer", "Engineering"),
    (11, "Admin User", "admin", "IT"),
]
PRIYA, MARCUS, DANA, JAKE, TOM, AISHA, SOFIA, KEN, LENA, OMAR, ADMIN = range(1, 12)
AGENTS = [JAKE, TOM, AISHA]

WATCHLIST = [
    ("Jon Smith", "1958-03-14", "Freedonia", "Fictional Sanctions List", "Financing of prohibited trade"),
    ("Alexander Mercer", "1987-06-02", "Latveria", "Fictional Sanctions List", "Money laundering network"),
    ("Maria Delacroix", "1972-11-30", "Genovia", "Fictional PEP List", "Former minister of finance"),
    ("Hassan Ortega", "1969-01-08", "Elbonia", "Fictional Sanctions List", "Arms trafficking"),
    ("Chen Wei Long", "1980-09-19", "Sokovia", "Fictional Sanctions List", "Sanctions evasion"),
    ("Robert Kline", "1975-04-25", "Freedonia", "Fictional PEP List", "Central bank official"),
    ("Ingrid Volkova", "1983-12-12", "Latveria", "Fictional Sanctions List", "Shell company director"),
    ("Samuel Adeyemi", "1966-07-07", "Wakanda", "Fictional PEP List", "Regional governor"),
    ("Lucia Ferraro", "1990-02-17", "Genovia", "Fictional Sanctions List", "Fraud proceeds"),
    ("David Brandt", "1961-10-03", "Elbonia", "Fictional Sanctions List", "Terrorist financing"),
    ("Nadia Petrenko", "1978-05-21", "Sokovia", "Fictional PEP List", "Family of head of state"),
    ("Kenji Morita", "1985-08-29", "Freedonia", "Fictional Sanctions List", "Cyber-enabled fraud"),
    ("Elena Ruiz", "1993-03-03", "Latveria", "Fictional Sanctions List", "Money mule network"),
    ("Paul Okafor", "1970-06-15", "Wakanda", "Fictional Sanctions List", "Bribery"),
    ("Grace Holloway", "1988-01-27", "Genovia", "Fictional PEP List", "State enterprise executive"),
]

FLAG_TYPES = {
    "sanctions_name_match": "Name similar to a sanctions list entry",
    "pep_match": "Possible politically exposed person",
    "selfie_mismatch": "Selfie does not match ID photo",
    "doc_expired": "ID document is expired",
    "address_mismatch": "Address does not match records",
    "duplicate_account": "Possible duplicate of an existing account",
}

REFUND_REASONS = ["duplicate_charge", "fee_error", "service_issue", "fraud_claim", "goodwill", "cancelled"]

SETTINGS = [
    ("refund_auto_approve_max_cents", "5000", "Refunds up to this amount are approved automatically"),
    ("refund_lead_max_cents", "50000", "Refunds up to this amount need a support lead; above it, lead then finance"),
    ("kyc_second_review_risk_score", "70", "KYC cases at or above this risk score need a second reviewer to approve"),
    ("kyc_sla_hours", "48", "Target hours to resolve a KYC case"),
    ("face_match_pass_score", "45", "Face match scores (0-100) at or above this count as likely the same person"),
    ("watchlist_review_pct", "85", "Signups whose name is at least this % similar to a watchlist entry go to manual review"),
    ("ocr_name_match_pct", "95", "Name read from the ID must be at least this % similar to the name the customer typed"),
    ("face_match_review_score", "30", "Face match scores from this up to the pass score need a reviewer's judgment; below it, likely different people"),
]


# Cases whose customers have sample signup documents: case id -> (ID photo face, selfie face).
# Case 2's selfie is a different person, so the face match flags it.
SIGNUP_DOCS = {1: ("face_m1.jpg", "face_m1.jpg"), 2: ("face_m2.jpg", "face_m3.jpg"), 3: ("face_f1.jpg", "face_f1.jpg")}
# More open cases get documents; these customers are renamed to fit the face.
EXPIRED_ID_CASES = {10: "2024-03-31"}
EXTRA_DOC_CUSTOMERS = [("face_f2.jpg", "Mei Tanaka"), ("face_m4.jpg", "Daniel Novak"),
                       ("face_f3.jpg", "Emily Carter"), ("face_f4.jpg", "Sarah Whitman")]


def iso(dt):
    return dt.isoformat(timespec="seconds")


def migrate():
    """Bring a database created by an older version up to date without wiping its data."""
    conn = connect()
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        conn.executescript(f.read())
    for table, col in (("face_checks", "ocr_json"), ("customers", "phone")):
        if col not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    conn.executemany("INSERT OR IGNORE INTO settings VALUES (?, ?, ?)", SETTINGS)
    conn.commit()
    conn.close()


def signup_face_checks(docs, customers, cases):
    """Render each customer's ID card and selfie and run the face match, as the vendor would at signup."""
    settings = {k: int(v) for k, v, _ in SETTINGS}
    checks = {}
    try:
        for cid, (id_face, selfie_face) in docs.items():
            c = customers[cases[cid - 1]["customer_id"] - 1]
            id_img = kyc_docs.id_card(id_face, c["name"], c["dob"], f"D{cid:03d}{c['ssn_last4']}", c["state"],
                                      expires=EXPIRED_ID_CASES.get(cid, "2031-12-31"))
            selfie_img = kyc_docs.selfie(selfie_face, seed=cid)
            result = face_match.compare(id_img, selfie_img)
            pct = max(0, round(result["score"] * 100))
            checks[cid] = dict(result, id_image=id_img, selfie_image=selfie_img, pct=pct, verdict=face_match.verdict(
                pct, settings["face_match_pass_score"], settings["face_match_review_score"]),
                ocr=ocr.check(id_img, c, settings["ocr_name_match_pct"]))
    except Exception as e:  # e.g. offline and the model files can't be downloaded
        print(f"Skipping sample KYC documents: {e}")
        return {}
    return checks


def build(db_path=DB_PATH):
    rng = random.Random(SEED)
    fake = Faker("en_US")
    Faker.seed(SEED)
    now = datetime.now().replace(microsecond=0)
    fee_engine_on = (now - timedelta(days=FEE_ENGINE_DAYS_AGO)).replace(hour=10, minute=12, second=0)

    if os.path.exists(db_path):
        os.remove(db_path)
    conn = connect()
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        conn.executescript(f.read())

    for uid, name, role, team in USERS:
        email = name.lower().replace(" ", ".") + "@acmepay.example"
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)", (uid, name, email, role, team))
    conn.executemany("INSERT INTO settings VALUES (?, ?, ?)", SETTINGS)
    for i, w in enumerate(WATCHLIST, 1):
        conn.execute("INSERT INTO watchlist VALUES (?, ?, ?, ?, ?, ?)", (i, *w))

    # Customers
    customers = []
    for cid in range(1, 201):
        name = fake.name()
        signup = now - timedelta(days=rng.randint(1, 400), hours=rng.randint(0, 23))
        customers.append({
            "id": cid,
            "name": name,
            "email": fake.free_email(),
            "dob": fake.date_of_birth(minimum_age=19, maximum_age=80).isoformat(),
            "address": fake.street_address() + ", " + fake.city(),
            "state": fake.state_abbr(include_territories=False),
            "ssn_last4": f"{rng.randint(0, 9999):04d}",
            "account_type": "business" if rng.random() < 0.15 else "personal",
            "status": "active",
            "signup_at": signup,
        })

    planted = {
        1: dict(name="John Smith", dob="1991-08-22", state="OH", address="1420 Maple Ave, Columbus"),
        2: dict(name="Alex Mercer", dob="1987-06-02", state="NJ", address="88 Harbor St, Newark"),
        3: dict(name="Grace Holloway", dob="1988-01-27", state="NY", address="510 W 52nd St, New York"),
    }
    for cid, fields in planted.items():
        customers[cid - 1].update(fields)
        customers[cid - 1]["signup_at"] = now - timedelta(days=1, hours=cid)

    # KYC cases: first 50 customers by id get a case; 1-3 are planted stories.
    kyc_cases = []
    open_statuses = ["new"] * 6 + ["in_review"] * 5 + ["needs_info"] * 3 + ["escalated"] * 2
    for cid in range(1, 51):
        c = customers[cid - 1]
        if cid == 1:
            flags = [{"type": "sanctions_name_match", "match_pct": 91, "watchlist_id": 1,
                      "detail": "Name 91% similar to 'Jon Smith' (DOB 1958-03-14, Freedonia)"}]
            risk, status, assigned = 64, "in_review", PRIYA
        elif cid == 2:
            flags = [{"type": "sanctions_name_match", "match_pct": 96, "watchlist_id": 2,
                      "detail": "Name 96% similar to 'Alexander Mercer' (DOB 1987-06-02, Latveria) - DOB matches"},
                     {"type": "selfie_mismatch", "detail": "Face match score 38/100"}]
            risk, status, assigned = 94, "new", None
        elif cid == 3:
            flags = [{"type": "pep_match", "match_pct": 88, "watchlist_id": 15,
                      "detail": "Possible match to 'Grace Holloway', state enterprise executive (Genovia)"},
                     {"type": "address_mismatch", "detail": "Address not found in credit header data"}]
            risk, status, assigned = 78, "in_review", PRIYA
        else:
            ftypes = rng.sample([k for k in FLAG_TYPES if k not in ("sanctions_name_match", "pep_match")],
                                k=rng.choice([1, 1, 2]))
            if rng.random() < 0.2:
                ftypes.append(rng.choice(["sanctions_name_match", "pep_match"]))
            flags = []
            for t in ftypes:
                f = {"type": t, "detail": FLAG_TYPES[t]}
                if t in ("sanctions_name_match", "pep_match"):
                    w = rng.randint(1, len(WATCHLIST))
                    f.update(match_pct=rng.randint(72, 89), watchlist_id=w,
                             detail=f"Name {rng.randint(72, 89)}% similar to '{WATCHLIST[w - 1][0]}'")
                if t == "selfie_mismatch":
                    f["detail"] = f"Face match score {rng.randint(35, 69)}/100"
                flags.append(f)
            risk = min(99, 25 + 18 * len(flags) + rng.randint(0, 25))
            status = open_statuses.pop() if open_statuses and cid <= 20 else rng.choice(["approved"] * 4 + ["rejected"])
            assigned = rng.choice([PRIYA, MARCUS]) if status not in ("new",) else None
        created = c["signup_at"] + timedelta(minutes=rng.randint(1, 30))
        case = {
            "id": cid, "customer_id": cid, "status": status, "assigned_to": assigned, "risk_score": risk,
            "flags": flags, "vendor_ref": f"MV-{rng.randint(100000, 999999)}",
            "requires_second_review": 1 if risk >= 70 else 0,
            "created_at": created, "sla_due_at": created + timedelta(hours=48),
            "decision": None, "decision_reason": None, "decision_note": None,
            "decided_by": None, "decided_at": None, "first_decision_by": None,
        }
        if status in ("approved", "rejected"):
            case["decided_at"] = created + timedelta(hours=rng.randint(2, 40))
            case["decided_by"] = assigned
            case["decision"] = status
            case["decision_reason"] = rng.choice(
                ["false_positive_cleared", "docs_verified"] if status == "approved"
                else ["identity_not_verified", "watchlist_confirmed", "fraud_suspected"])
            case["decision_note"] = (
                "Reviewed vendor results and documents; discrepancy explained." if status == "approved"
                else "Unable to verify identity after review.")
            if case["requires_second_review"] and status == "approved":
                case["first_decision_by"] = PRIYA
                case["decided_by"] = MARCUS
            c["status"] = "active" if status == "approved" else "rejected"
        else:
            c["status"] = "pending_kyc"
        kyc_cases.append(case)

    docs = dict(SIGNUP_DOCS)
    extra = [k for k in kyc_cases if k["id"] > 3 and k["status"] not in ("approved", "rejected")
             and not any(f["type"] == "selfie_mismatch" for f in k["flags"])]
    for k, (face, name) in zip(extra, EXTRA_DOC_CUSTOMERS):
        customers[k["customer_id"] - 1]["name"] = name
        docs[k["id"]] = (face, face)
    face_checks = signup_face_checks(docs, customers, kyc_cases)
    for cid, fc in face_checks.items():
        for f in kyc_cases[cid - 1]["flags"]:
            if f["type"] == "selfie_mismatch":
                f["detail"] = f"Face match score {fc['pct']}/100"
        if fc["ocr"]["expired"]:
            flags = [f for f in kyc_cases[cid - 1]["flags"] if f["type"] != "doc_expired"]
            flags.append({"type": "doc_expired", "detail": f"ID expired {fc['ocr']['fields']['expires']} (read by OCR)"})
            kyc_cases[cid - 1]["flags"] = flags

    for c in customers:
        conn.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c["id"], c["name"], c["email"], c["dob"], c["address"], c["state"], c["ssn_last4"],
             c["account_type"], c["status"], iso(c["signup_at"]), f"+1 212-555-01{c['id'] % 100:02d}"),
        )
    for k in kyc_cases:
        conn.execute(
            "INSERT INTO kyc_cases (id, customer_id, status, assigned_to, risk_score, flags_json, vendor_ref,"
            " requires_second_review, created_at, sla_due_at, first_decision_by, decision, decision_reason,"
            " decision_note, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (k["id"], k["customer_id"], k["status"], k["assigned_to"], k["risk_score"], json.dumps(k["flags"]),
             k["vendor_ref"], k["requires_second_review"], iso(k["created_at"]), iso(k["sla_due_at"]),
             k["first_decision_by"], k["decision"], k["decision_reason"], k["decision_note"], k["decided_by"],
             iso(k["decided_at"]) if k["decided_at"] else None),
        )
        audit(conn, None, "kyc_case_created", "kyc_case", k["id"], after={"status": "new", "risk_score": k["risk_score"]},
              reason="Created from MockVerify webhook", ts=iso(k["created_at"]))
        if k["decided_at"]:
            if k["first_decision_by"]:
                audit(conn, k["first_decision_by"], "kyc_first_approval", "kyc_case", k["id"],
                      before={"status": "in_review"}, after={"status": "pending_second_review"},
                      reason=k["decision_note"], ts=iso(k["decided_at"] - timedelta(hours=1)))
            audit(conn, k["decided_by"], f"kyc_{k['decision']}", "kyc_case", k["id"],
                  before={"status": "in_review"}, after={"status": k["decision"], "reason": k["decision_reason"]},
                  reason=k["decision_note"], ts=iso(k["decided_at"]))

    for cid, fc in face_checks.items():
        ts = iso(kyc_cases[cid - 1]["created_at"])
        details = {k: fc[k] for k in ("id_faces_found", "selfie_faces_found", "id_face_confidence", "selfie_face_confidence")}
        details["model"] = "OpenCV YuNet + SFace (cosine similarity)"
        cur = conn.execute(
            "INSERT INTO face_checks (case_id, run_by, source, created_at, score, verdict, id_image, id_image_type,"
            " selfie_image, selfie_image_type, id_face, selfie_face, details_json, ocr_json)"
            " VALUES (?, NULL, 'signup', ?, ?, ?, ?, 'image/jpeg', ?, 'image/jpeg', ?, ?, ?, ?)",
            (cid, ts, fc["score"], fc["verdict"], fc["id_image"], fc["selfie_image"], fc["id_face_jpg"],
             fc["selfie_face_jpg"], json.dumps(details), json.dumps(fc["ocr"])),
        )
        audit(conn, None, "face_match_run", "kyc_case", cid,
              after={"face_check_id": cur.lastrowid, "score": fc["pct"], "verdict": fc["verdict"]},
              reason="MockVerify check at signup", ts=ts)

    # Transactions for active customers over 90 days
    active_ids = [c["id"] for c in customers if c["status"] == "active"]
    txns = []

    def add_txn(cid, ttype, cents, desc, when):
        txns.append({"id": len(txns) + 1, "customer_id": cid, "type": ttype, "amount_cents": cents,
                     "description": desc, "created_at": when})
        return txns[-1]

    merchants = ["Grocery Mart", "City Transit", "Coffee Co", "Online Books", "Gas & Go", "Home Supply",
                 "Streaming Plus", "Airline Tix", "Pharmacy One", "Electronics Hub"]
    for _ in range(1500):
        when = now - timedelta(days=rng.uniform(0, 90))
        cid = rng.choice(active_ids)
        r = rng.random()
        if r < 0.6:
            add_txn(cid, "card_payment", rng.randint(300, 40000), rng.choice(merchants), when)
        elif r < 0.8:
            add_txn(cid, "transfer", rng.randint(2000, 200000), "Transfer to " + fake.first_name(), when)
        elif r < 0.92:
            add_txn(cid, "subscription", rng.choice([999, 1999]), "AcmePay Plus subscription", when)
        else:
            add_txn(cid, "fee", rng.choice([3500, 2500, 300]), rng.choice(["Late payment fee", "Wire fee", "FX fee"]), when)

    # Fee engine bug: after the flag flipped, some customers were wrongly charged a $12 service fee.
    bad_fee_txns = []
    for _ in range(140):
        when = fee_engine_on + timedelta(hours=rng.uniform(1, (now - fee_engine_on).total_seconds() / 3600 - 1))
        bad_fee_txns.append(add_txn(rng.choice(active_ids), "fee", 1200, "Monthly service fee", when))

    txns.sort(key=lambda t: t["created_at"])
    for i, t in enumerate(txns, 1):
        t["id"] = i
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)",
                     (i, t["customer_id"], t["type"], t["amount_cents"], t["description"], iso(t["created_at"])))

    # Refunds
    auto_max, lead_max = 5000, 50000
    refunds = []

    def add_refund(txn, reason, agent, when, status=None, amount=None, note=None):
        amount = amount or txn["amount_cents"]
        refunds.append({"txn": txn, "reason": reason, "agent": agent, "when": when, "status": status,
                        "amount": amount, "note": note})

    non_fee = [t for t in txns if t["type"] != "fee" and t["created_at"] < now - timedelta(days=1)]
    for _ in range(60):
        t = rng.choice(non_fee)
        agent = TOM if rng.random() < 0.7 else rng.choice([JAKE, AISHA])
        reason = rng.choice(["duplicate_charge", "service_issue", "fraud_claim", "goodwill", "cancelled", "fee_error"])
        amt = t["amount_cents"] if rng.random() < 0.7 else max(500, t["amount_cents"] // 2)
        add_refund(t, reason, agent, t["created_at"] + timedelta(hours=rng.uniform(2, 72)), amount=min(amt, 150000))

    for t in rng.sample(bad_fee_txns, 48):
        add_refund(t, "fee_error", rng.choices(AGENTS, weights=[1, 2, 1])[0], t["created_at"] + timedelta(hours=rng.uniform(4, 60)),
                   note="Customer charged $12 monthly service fee they did not sign up for")

    repeat_cid = active_ids[7]
    repeat_txns = [add_txn(repeat_cid, "card_payment", rng.randint(2000, 4800), rng.choice(merchants),
                           now - timedelta(days=d)) for d in (28, 22, 17, 11, 6, 3)]
    for t in repeat_txns:
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)",
                     (t["id"], t["customer_id"], t["type"], t["amount_cents"], t["description"], iso(t["created_at"])))
        add_refund(t, rng.choice(["service_issue", "goodwill"]), rng.choice(AGENTS), t["created_at"] + timedelta(hours=5),
                   note="Customer says item never arrived")

    big1 = add_txn(active_ids[20], "transfer", 75000, "Transfer to Maya", now - timedelta(days=2))
    big2 = add_txn(active_ids[33], "transfer", 120000, "Transfer to Liam", now - timedelta(days=3))
    for t in (big1, big2):
        conn.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?)",
                     (t["id"], t["customer_id"], t["type"], t["amount_cents"], t["description"], iso(t["created_at"])))
    add_refund(big1, "duplicate_charge", JAKE, now - timedelta(hours=20), status="pending_approval",
               note="Transfer was submitted twice; customer has both confirmations")
    add_refund(big2, "fraud_claim", AISHA, now - timedelta(days=1, hours=4), status="pending_approval",
               note="Customer reports account takeover; transfer not authorized")

    refunds.sort(key=lambda r: r["when"])
    for rid, r in enumerate(refunds, 1):
        amount = r["amount"]
        steps = [] if amount <= auto_max else (["support_lead"] if amount <= lead_max else ["support_lead", "finance"])
        status = r["status"]
        if status is None:
            if r["when"] > now - timedelta(hours=30) and steps:
                status = "pending_approval"
            else:
                status = "rejected" if steps and rng.random() < 0.1 else "sent"
        resolved = None if status == "pending_approval" else r["when"] + timedelta(hours=rng.uniform(0.1, 20))
        note = r["note"] or {"duplicate_charge": "Charged twice for the same purchase",
                             "fee_error": "Fee charged in error", "service_issue": "Service problem reported by customer",
                             "fraud_claim": "Customer did not authorize this charge", "goodwill": "Goodwill credit",
                             "cancelled": "Order cancelled by customer"}[r["reason"]]
        conn.execute("INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (rid, r["txn"]["id"], r["txn"]["customer_id"], amount, r["reason"], note, status, r["agent"],
                      iso(r["when"]), iso(resolved) if resolved else None))
        audit(conn, r["agent"], "refund_requested", "refund", rid, after={"amount_cents": amount, "reason": r["reason"]},
              reason=note, ts=iso(r["when"]))
        for step, role in enumerate(steps, 1):
            approver = SOFIA if role == "support_lead" else KEN
            if status == "pending_approval":
                if rid % 2 == 0 and step == 1 and len(steps) == 2:
                    a_status, by, at = "approved", SOFIA, iso(r["when"] + timedelta(hours=2))
                else:
                    a_status, by, at = "pending", None, None
                    conn.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status)"
                                 " VALUES ('refund', ?, ?, ?, 'pending')", (rid, step, role))
                    break
            else:
                a_status = "rejected" if status == "rejected" and step == len(steps) else "approved"
                by, at = approver, iso(resolved)
            conn.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status, decided_by,"
                         " decided_at, comment) VALUES ('refund', ?, ?, ?, ?, ?, ?, ?)",
                         (rid, step, role, a_status, by, at, "Looks good" if a_status == "approved" else "Insufficient evidence"))
            audit(conn, by, f"refund_step_{a_status}", "refund", rid, after={"step": step, "role": role}, ts=at)
        if status in ("sent", "rejected") and not steps:
            audit(conn, None, "refund_auto_approved", "refund", rid, after={"status": "sent"},
                  reason="Under auto-approve threshold", ts=iso(resolved))

    # Feature flags
    flags = [
        ("new_fee_engine", "Calculate account fees with the new pricing service", LENA, 1, 100, [], 0, fee_engine_on),
        ("instant_transfers", "Instant bank transfers for eligible customers", LENA, 1, 50, [], 0, now - timedelta(days=9)),
        ("international_payments", "Send money abroad (beta)", LENA, 1, 0, [12, 47, 88], 0, now - timedelta(days=30)),
        ("partner_bank_b_transfers", "Route transfers through Partner Bank B", OMAR, 1, 100, [], 1, now - timedelta(days=60)),
        ("card_payments_kill_switch", "Disable all card payments (emergency only)", OMAR, 0, 0, [], 1, now - timedelta(days=120)),
        ("new_onboarding_flow", "Redesigned signup and KYC flow", LENA, 1, 25, [], 0, now - timedelta(days=5)),
        ("savings_accounts", "High-yield savings product", LENA, 0, 0, [3, 9], 0, now - timedelta(days=14)),
        ("dark_mode", "Dark mode in the mobile app", OMAR, 1, 100, [], 0, now - timedelta(days=200)),
        ("business_invoicing", "Invoicing for business accounts", LENA, 1, 10, [], 0, now - timedelta(days=12)),
        ("crypto_purchases", "Buy and sell crypto (not launched)", LENA, 0, 0, [], 1, now - timedelta(days=90)),
        ("referral_bonus_v2", "Referral program with $50 bonus", LENA, 1, 100, [], 0, now - timedelta(days=45)),
        ("statement_pdf_v2", "New PDF statement generator", OMAR, 1, 100, [], 0, now - timedelta(days=33)),
    ]
    for i, (key, desc, owner, enabled, pct, targets, critical, updated) in enumerate(flags, 1):
        conn.execute("INSERT INTO feature_flags VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (i, key, desc, owner, enabled, pct, json.dumps(targets), critical, iso(updated)))
        if key == "new_fee_engine":
            audit(conn, LENA, "flag_updated", "feature_flag", i, before={"enabled": 0, "rollout_pct": 0},
                  after={"enabled": 1, "rollout_pct": 100}, reason="Launch new fee engine to everyone", ts=iso(updated))
        else:
            audit(conn, owner, "flag_updated", "feature_flag", i, after={"enabled": enabled, "rollout_pct": pct},
                  reason="Initial configuration", ts=iso(updated))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    build()
    print(f"Seeded {DB_PATH}")
