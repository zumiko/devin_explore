import difflib
import json
import random
from datetime import datetime, timedelta

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

import face_match
import ocr
import seed
from db import audit, connect, get_setting, now_iso

app = Flask(__name__)
app.secret_key = "demo-only-not-secret"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

ALL_ROLES = {"kyc_analyst", "kyc_senior", "compliance_lead", "support_agent", "support_lead",
             "finance", "pm", "engineer", "admin"}
KYC_ROLES = {"kyc_analyst", "kyc_senior", "compliance_lead", "admin"}
PERMS = {
    "kyc_view": KYC_ROLES,
    "kyc_second_review": {"kyc_senior", "compliance_lead", "admin"},
    "customer_view": KYC_ROLES | {"support_agent", "support_lead", "finance"},
    "customer_pii": KYC_ROLES,
    "refund_view": {"support_agent", "support_lead", "finance", "compliance_lead", "admin"},
    "refund_request": {"support_agent", "support_lead", "admin"},
    "refund_dashboard": {"support_lead", "finance", "pm", "engineer", "compliance_lead", "admin"},
    "approvals_view": {"support_lead", "finance", "admin"},
    "flag_view": ALL_ROLES,
    "flag_edit": {"pm", "engineer", "admin"},
    "flag_edit_critical": {"engineer", "admin"},
    "audit_view": {"compliance_lead", "support_lead", "finance", "engineer", "admin"},
    "settings_edit": {"admin"},
}
KYC_REASONS = {
    "approve": ["false_positive_cleared", "docs_verified", "additional_docs_received"],
    "reject": ["identity_not_verified", "watchlist_confirmed", "fraud_suspected"],
    "request_info": ["need_clearer_id", "need_proof_of_address", "need_selfie_retake"],
    "escalate": ["complex_match", "possible_pep", "needs_senior_judgment"],
}
REFUND_REASONS = seed.REFUND_REASONS
OPEN_KYC = ("new", "in_review", "needs_info", "escalated", "pending_second_review")


def can(perm):
    return g.user["role"] in PERMS[perm]


def require(perm):
    if not can(perm):
        abort(403)


@app.before_request
def load_user():
    g.db = connect()
    uid = session.get("user_id", 1)
    g.user = g.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    g.users = g.db.execute("SELECT * FROM users ORDER BY id").fetchall()


@app.teardown_request
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject():
    pending = 0
    if can("approvals_view"):
        pending = len(pending_approvals_for(g.user))
    return {"can": can, "me": g.user, "all_users": g.users, "pending_count": pending}


@app.template_filter("money")
def money(cents):
    return f"${cents / 100:,.2f}"


@app.template_filter("dt")
def fmt_dt(value):
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%b %d, %H:%M")


@app.template_filter("label")
def label(value):
    return (value or "").replace("_", " ")


@app.template_filter("fromjson")
def fromjson(value):
    return json.loads(value) if value else None


def history(entity_type, entity_id):
    return g.db.execute(
        "SELECT a.*, u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id"
        " WHERE entity_type = ? AND entity_id = ? ORDER BY ts DESC, a.id DESC",
        (entity_type, entity_id),
    ).fetchall()


def pending_approvals_for(user):
    rows = g.db.execute(
        "SELECT a.*, r.amount_cents, r.reason, r.note, r.requested_by, r.customer_id, c.name AS customer,"
        " u.name AS requester FROM approvals a JOIN refunds r ON a.entity_type = 'refund' AND r.id = a.entity_id"
        " JOIN customers c ON c.id = r.customer_id JOIN users u ON u.id = r.requested_by"
        " WHERE a.status = 'pending' ORDER BY r.created_at"
    ).fetchall()
    return [r for r in rows if user["role"] == "admin" or r["required_role"] == user["role"]]


@app.post("/switch-user")
def switch_user():
    session["user_id"] = int(request.form["user_id"])
    return redirect(request.referrer or url_for("index"))


@app.post("/reset")
def reset():
    g.db.close()
    seed.build()
    g.db = connect()
    flash("Demo data reset.")
    return redirect(url_for("index"))


@app.get("/")
def index():
    db = g.db
    stats = {
        "kyc_open": db.execute(f"SELECT COUNT(*) FROM kyc_cases WHERE status IN {OPEN_KYC}").fetchone()[0],
        "kyc_overdue": db.execute(
            f"SELECT COUNT(*) FROM kyc_cases WHERE status IN {OPEN_KYC} AND sla_due_at < ?", (now_iso(),)
        ).fetchone()[0],
        "refunds_pending": db.execute("SELECT COUNT(*) FROM refunds WHERE status = 'pending_approval'").fetchone()[0],
        "refunds_30d": db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE status = 'sent' AND created_at > ?",
            ((datetime.now() - timedelta(days=30)).isoformat(),),
        ).fetchone()[0],
        "flags_on": db.execute("SELECT COUNT(*) FROM feature_flags WHERE enabled = 1").fetchone()[0],
    }
    my_cases = db.execute(
        f"SELECT k.*, c.name FROM kyc_cases k JOIN customers c ON c.id = k.customer_id"
        f" WHERE k.assigned_to = ? AND k.status IN {OPEN_KYC} ORDER BY k.sla_due_at",
        (g.user["id"],),
    ).fetchall()
    approvals = pending_approvals_for(g.user) if can("approvals_view") else []
    return render_template("index.html", stats=stats, my_cases=my_cases, approvals=approvals)


# ---------- KYC ----------

@app.get("/kyc")
def kyc_list():
    require("kyc_view")
    view = request.args.get("view", "open")
    where, params = "", []
    if view == "open":
        where = f"WHERE k.status IN {OPEN_KYC}"
    elif view == "mine":
        where = f"WHERE k.status IN {OPEN_KYC} AND k.assigned_to = ?"
        params = [g.user["id"]]
    elif view == "closed":
        where = "WHERE k.status IN ('approved', 'rejected')"
    cases = g.db.execute(
        "SELECT k.*, c.name, c.account_type, u.name AS assignee FROM kyc_cases k"
        " JOIN customers c ON c.id = k.customer_id LEFT JOIN users u ON u.id = k.assigned_to "
        f"{where} ORDER BY CASE WHEN k.status IN {OPEN_KYC} THEN 0 ELSE 1 END, k.risk_score DESC, k.sla_due_at",
        params,
    ).fetchall()
    return render_template("kyc_list.html", cases=cases, view=view, now=now_iso())


def load_case(case_id):
    case = g.db.execute(
        "SELECT k.*, u.name AS assignee, f.name AS first_decider, d.name AS decider FROM kyc_cases k"
        " LEFT JOIN users u ON u.id = k.assigned_to LEFT JOIN users f ON f.id = k.first_decision_by"
        " LEFT JOIN users d ON d.id = k.decided_by WHERE k.id = ?",
        (case_id,),
    ).fetchone()
    if not case:
        abort(404)
    return case


@app.get("/kyc/<int:case_id>")
def kyc_case(case_id):
    require("kyc_view")
    case = load_case(case_id)
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone()
    flags = json.loads(case["flags_json"])
    for f in flags:
        if f.get("watchlist_id"):
            f["watchlist"] = g.db.execute("SELECT * FROM watchlist WHERE id = ?", (f["watchlist_id"],)).fetchone()
    face_checks = g.db.execute(
        "SELECT f.id, f.created_at, f.score, f.verdict, f.source, f.details_json, f.ocr_json, u.name AS run_by_name"
        " FROM face_checks f LEFT JOIN users u ON u.id = f.run_by WHERE f.case_id = ? ORDER BY f.id DESC",
        (case_id,),
    ).fetchall()
    return render_template("kyc_case.html", case=case, customer=customer, flags=flags,
                           reasons=KYC_REASONS, history=history("kyc_case", case_id), face_checks=face_checks,
                           face_pass=int(get_setting(g.db, "face_match_pass_score")),
                           face_review=int(get_setting(g.db, "face_match_review_score")))


@app.post("/kyc/<int:case_id>/face-match")
def kyc_face_match(case_id):
    require("kyc_view")
    case = load_case(case_id)
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone()
    id_file, selfie_file = request.files.get("id_image"), request.files.get("selfie")
    if not id_file or not id_file.filename or not selfie_file or not selfie_file.filename:
        flash("Upload both an ID photo and a selfie.", "error")
        return redirect(url_for("kyc_case", case_id=case_id))
    id_bytes, selfie_bytes = id_file.read(), selfie_file.read()
    try:
        result = face_match.compare(id_bytes, selfie_bytes)
    except face_match.FaceMatchError as e:
        flash(f"Face match failed: {e}", "error")
        return redirect(url_for("kyc_case", case_id=case_id))
    score_pct = max(0, round(result["score"] * 100))
    verdict = face_match.verdict(score_pct, int(get_setting(g.db, "face_match_pass_score")),
                                 int(get_setting(g.db, "face_match_review_score")))
    details = {k: result[k] for k in ("id_faces_found", "selfie_faces_found",
                                      "id_face_confidence", "selfie_face_confidence")}
    details["model"] = "OpenCV YuNet + SFace (cosine similarity)"
    id_read = ocr.check(id_bytes, customer, int(get_setting(g.db, "ocr_name_match_pct")))
    cur = g.db.execute(
        "INSERT INTO face_checks (case_id, run_by, source, created_at, score, verdict, id_image, id_image_type,"
        " selfie_image, selfie_image_type, id_face, selfie_face, details_json, ocr_json)"
        " VALUES (?, ?, 'reviewer_upload', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (case_id, g.user["id"], now_iso(), result["score"], verdict, id_bytes, id_file.mimetype or "image/jpeg",
         selfie_bytes, selfie_file.mimetype or "image/jpeg", result["id_face_jpg"], result["selfie_face_jpg"],
         json.dumps(details), json.dumps(id_read)),
    )
    audit(g.db, g.user["id"], "face_match_run", "kyc_case", case_id,
          after={"face_check_id": cur.lastrowid, "score": score_pct, "verdict": verdict})
    g.db.commit()
    flash(f"Face match score {score_pct}/100: {label(verdict)}.")
    return redirect(url_for("kyc_case", case_id=case_id) + "#face-match")


def watchlist_hit(name):
    best, best_pct = None, 0
    for w in g.db.execute("SELECT * FROM watchlist").fetchall():
        pct = round(difflib.SequenceMatcher(None, name.lower(), w["name"].lower()).ratio() * 100)
        if pct > best_pct:
            best, best_pct = w, pct
    return (best, best_pct) if best_pct >= int(get_setting(g.db, "watchlist_review_pct")) else (None, best_pct)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Customer-facing account opening: runs the identity checks and either approves instantly or opens a KYC case."""
    if request.method == "GET":
        return render_template("signup.html", result=None, form={})
    form = {k: request.form.get(k, "").strip() for k in ("name", "email", "dob", "address", "state", "ssn_last4")}
    id_file, selfie_file = request.files.get("id_image"), request.files.get("selfie")
    if not all(form.values()) or not id_file or not id_file.filename or not selfie_file or not selfie_file.filename:
        flash("Please fill in every field and add both photos.", "error")
        return render_template("signup.html", result=None, form=form)
    id_bytes, selfie_bytes = id_file.read(), selfie_file.read()
    now = datetime.now().replace(microsecond=0)
    flags, face, risk = [], None, 30
    pass_score = int(get_setting(g.db, "face_match_pass_score"))
    try:
        face = face_match.compare(id_bytes, selfie_bytes)
        face["pct"] = max(0, round(face["score"] * 100))
        face["verdict"] = face_match.verdict(face["pct"], pass_score, int(get_setting(g.db, "face_match_review_score")))
        if face["verdict"] != "likely_same_person":
            flags.append({"type": "selfie_mismatch", "detail": f"Face match score {face['pct']}/100"})
            risk += 35 if face["verdict"] == "likely_different_people" else 20
    except face_match.FaceMatchError as e:
        flags.append({"type": "selfie_mismatch", "detail": f"Automatic face match failed: {e}"})
        risk += 25
    id_read = ocr.check(id_bytes, form, int(get_setting(g.db, "ocr_name_match_pct")))
    if id_read["expired"]:
        flags.append({"type": "doc_expired", "detail": f"ID expired {id_read['fields']['expires']} (read by OCR)"})
        risk += 30
    if id_read["problems"]:
        flags.append({"type": "id_details_mismatch", "detail": "; ".join(id_read["problems"])})
        risk += 25
    hit, hit_pct = watchlist_hit(form["name"])
    if hit:
        kind = "pep_match" if "PEP" in hit["list_name"] else "sanctions_name_match"
        flags.append({"type": kind, "match_pct": hit_pct, "watchlist_id": hit["id"],
                      "detail": f"Name {hit_pct}% similar to '{hit['name']}' (DOB {hit['dob']}, {hit['country']})"})
        risk += 45 + (20 if hit["dob"] == form["dob"] else 0)
    approved = not flags
    risk = min(99, risk if flags else 10)

    cid = g.db.execute(
        "INSERT INTO customers (name, email, dob, address, state, ssn_last4, account_type, status, signup_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'personal', ?, ?)",
        (form["name"], form["email"], form["dob"], form["address"], form["state"].upper()[:2], form["ssn_last4"][-4:],
         "active" if approved else "pending_kyc", now.isoformat()),
    ).lastrowid
    case_id = g.db.execute(
        "INSERT INTO kyc_cases (customer_id, status, risk_score, flags_json, vendor_ref, requires_second_review,"
        " created_at, sla_due_at, decision, decision_reason, decision_note, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, "approved" if approved else "new", risk, json.dumps(flags), f"MV-{random.randint(100000, 999999)}",
         int(risk >= int(get_setting(g.db, "kyc_second_review_risk_score"))), now.isoformat(),
         (now + timedelta(hours=int(get_setting(g.db, "kyc_sla_hours")))).isoformat(),
         "approved" if approved else None, "auto_approved" if approved else None,
         "All automatic checks passed at signup." if approved else None, now.isoformat() if approved else None),
    ).lastrowid
    audit(g.db, None, "kyc_case_created", "kyc_case", case_id, after={"status": "new", "risk_score": risk},
          reason="Created from customer signup")
    if face:
        details = {k: face[k] for k in ("id_faces_found", "selfie_faces_found", "id_face_confidence", "selfie_face_confidence")}
        details["model"] = "OpenCV YuNet + SFace (cosine similarity)"
        fc_id = g.db.execute(
            "INSERT INTO face_checks (case_id, run_by, source, created_at, score, verdict, id_image, id_image_type,"
            " selfie_image, selfie_image_type, id_face, selfie_face, details_json, ocr_json)"
            " VALUES (?, NULL, 'signup', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, now.isoformat(), face["score"], face["verdict"], id_bytes, id_file.mimetype or "image/jpeg",
             selfie_bytes, selfie_file.mimetype or "image/jpeg", face["id_face_jpg"], face["selfie_face_jpg"],
             json.dumps(details), json.dumps(id_read)),
        ).lastrowid
        audit(g.db, None, "face_match_run", "kyc_case", case_id,
              after={"face_check_id": fc_id, "score": face["pct"], "verdict": face["verdict"]}, reason="Check at signup")
    if approved:
        audit(g.db, None, "kyc_auto_approved", "kyc_case", case_id, before={"status": "new"},
              after={"status": "approved"}, reason="All automatic checks passed at signup")
    g.db.commit()
    return render_template("signup.html", result="approved" if approved else "review", name=form["name"],
                           case_id=case_id, form={})


FACE_IMAGE_COLUMNS = {"id": ("id_image", "id_image_type"), "selfie": ("selfie_image", "selfie_image_type"),
                      "id_face": ("id_face", None), "selfie_face": ("selfie_face", None)}


@app.get("/kyc/face-check/<int:check_id>/<kind>")
def face_check_image(check_id, kind):
    require("kyc_view")
    if kind not in FACE_IMAGE_COLUMNS:
        abort(404)
    col, type_col = FACE_IMAGE_COLUMNS[kind]
    mime = type_col or "'image/jpeg'"
    row = g.db.execute(f"SELECT {col} AS data, {mime} AS mime FROM face_checks WHERE id = ?", (check_id,)).fetchone()
    if not row:
        abort(404)
    return Response(row["data"], mimetype=row["mime"])


@app.post("/kyc/<int:case_id>/claim")
def kyc_claim(case_id):
    require("kyc_view")
    case = load_case(case_id)
    if case["status"] == "escalated" and not can("kyc_second_review"):
        flash("Escalated cases can only be picked up by a senior reviewer.", "error")
        return redirect(url_for("kyc_case", case_id=case_id))
    new_status = "in_review" if case["status"] in ("new", "escalated") else case["status"]
    g.db.execute("UPDATE kyc_cases SET assigned_to = ?, status = ? WHERE id = ?", (g.user["id"], new_status, case_id))
    audit(g.db, g.user["id"], "kyc_claimed", "kyc_case", case_id,
          before={"assigned_to": case["assignee"], "status": case["status"]},
          after={"assigned_to": g.user["name"], "status": new_status})
    g.db.commit()
    return redirect(url_for("kyc_case", case_id=case_id))


@app.post("/kyc/<int:case_id>/decide")
def kyc_decide(case_id):
    require("kyc_view")
    case = load_case(case_id)
    decision = request.form.get("decision")
    reason = request.form.get("reason", "")
    note = request.form.get("note", "").strip()
    if decision not in KYC_REASONS or reason not in KYC_REASONS[decision] or not note:
        flash("Pick a decision, a matching reason, and write a note.", "error")
        return redirect(url_for("kyc_case", case_id=case_id))
    if case["status"] in ("approved", "rejected"):
        flash("This case is already closed.", "error")
        return redirect(url_for("kyc_case", case_id=case_id))
    if case["assigned_to"] != g.user["id"] and case["status"] != "pending_second_review":
        flash("Claim the case before making a decision.", "error")
        return redirect(url_for("kyc_case", case_id=case_id))

    updates = {}
    customer_status = None
    if decision == "approve":
        if case["status"] == "pending_second_review":
            if not can("kyc_second_review"):
                flash("Only a senior reviewer can give the second approval.", "error")
                return redirect(url_for("kyc_case", case_id=case_id))
            if case["first_decision_by"] == g.user["id"]:
                flash("The second approval must come from a different person.", "error")
                return redirect(url_for("kyc_case", case_id=case_id))
            updates = {"status": "approved"}
            customer_status = "active"
        elif case["requires_second_review"]:
            updates = {"status": "pending_second_review", "first_decision_by": g.user["id"]}
            flash("High-risk case: sent for a second reviewer's approval.")
        else:
            updates = {"status": "approved"}
            customer_status = "active"
    elif decision == "reject":
        updates = {"status": "rejected"}
        customer_status = "rejected"
    elif decision == "request_info":
        updates = {"status": "needs_info"}
    elif decision == "escalate":
        updates = {"status": "escalated", "assigned_to": None}

    if updates["status"] in ("approved", "rejected"):
        updates.update(decision=updates["status"], decided_by=g.user["id"], decided_at=now_iso())
    updates.update(decision_reason=reason, decision_note=note)
    sets = ", ".join(f"{k} = ?" for k in updates)
    g.db.execute(f"UPDATE kyc_cases SET {sets} WHERE id = ?", (*updates.values(), case_id))
    if customer_status:
        g.db.execute("UPDATE customers SET status = ? WHERE id = ?", (customer_status, case["customer_id"]))
    audit(g.db, g.user["id"], f"kyc_{decision}", "kyc_case", case_id,
          before={"status": case["status"]}, after={"status": updates["status"], "reason": reason}, reason=note)
    g.db.commit()
    return redirect(url_for("kyc_case", case_id=case_id))


# ---------- Customers ----------

@app.get("/customers")
def customers():
    require("customer_view")
    q = request.args.get("q", "").strip()
    rows = g.db.execute(
        "SELECT * FROM customers WHERE name LIKE ? OR email LIKE ? OR CAST(id AS TEXT) = ? ORDER BY name LIMIT 100",
        (f"%{q}%", f"%{q}%", q),
    ).fetchall()
    return render_template("customers.html", customers=rows, q=q)


@app.get("/customers/<int:customer_id>")
def customer(customer_id):
    require("customer_view")
    c = g.db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not c:
        abort(404)
    txns = g.db.execute(
        "SELECT t.*, COALESCE((SELECT SUM(amount_cents) FROM refunds r WHERE r.transaction_id = t.id"
        " AND r.status != 'rejected'), 0) AS refunded FROM transactions t WHERE customer_id = ?"
        " ORDER BY created_at DESC LIMIT 50",
        (customer_id,),
    ).fetchall()
    refunds = g.db.execute(
        "SELECT r.*, u.name AS requester FROM refunds r JOIN users u ON u.id = r.requested_by"
        " WHERE customer_id = ? ORDER BY created_at DESC",
        (customer_id,),
    ).fetchall()
    case = g.db.execute("SELECT * FROM kyc_cases WHERE customer_id = ?", (customer_id,)).fetchone()
    return render_template("customer.html", c=c, txns=txns, refunds=refunds, case=case)


# ---------- Refunds ----------

@app.get("/refunds")
def refunds():
    require("refund_view")
    status = request.args.get("status", "")
    where = "WHERE r.status = ?" if status else ""
    rows = g.db.execute(
        "SELECT r.*, c.name AS customer, u.name AS requester FROM refunds r JOIN customers c ON c.id = r.customer_id"
        f" JOIN users u ON u.id = r.requested_by {where} ORDER BY r.created_at DESC LIMIT 200",
        (status,) if status else (),
    ).fetchall()
    return render_template("refunds.html", refunds=rows, status=status)


@app.route("/refunds/new/<int:txn_id>", methods=["GET", "POST"])
def refund_new(txn_id):
    require("refund_request")
    txn = g.db.execute(
        "SELECT t.*, c.name AS customer FROM transactions t JOIN customers c ON c.id = t.customer_id WHERE t.id = ?",
        (txn_id,),
    ).fetchone()
    if not txn:
        abort(404)
    already = g.db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE transaction_id = ? AND status != 'rejected'", (txn_id,)
    ).fetchone()[0]
    remaining = txn["amount_cents"] - already
    auto_max = int(get_setting(g.db, "refund_auto_approve_max_cents"))
    lead_max = int(get_setting(g.db, "refund_lead_max_cents"))
    if request.method == "GET":
        return render_template("refund_new.html", txn=txn, remaining=remaining, reasons=REFUND_REASONS,
                               auto_max=auto_max, lead_max=lead_max)

    try:
        amount = round(float(request.form["amount"]) * 100)
    except ValueError:
        amount = 0
    reason = request.form.get("reason")
    note = request.form.get("note", "").strip()
    if amount <= 0 or amount > remaining or reason not in REFUND_REASONS or not note:
        flash(f"Enter an amount up to {money(remaining)}, a reason, and a note.", "error")
        return redirect(url_for("refund_new", txn_id=txn_id))

    steps = [] if amount <= auto_max else (["support_lead"] if amount <= lead_max else ["support_lead", "finance"])
    status = "sent" if not steps else "pending_approval"
    cur = g.db.execute(
        "INSERT INTO refunds (transaction_id, customer_id, amount_cents, reason, note, status, requested_by,"
        " created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (txn_id, txn["customer_id"], amount, reason, note, status, g.user["id"], now_iso(),
         now_iso() if status == "sent" else None),
    )
    rid = cur.lastrowid
    audit(g.db, g.user["id"], "refund_requested", "refund", rid, after={"amount_cents": amount, "reason": reason}, reason=note)
    if steps:
        g.db.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status)"
                     " VALUES ('refund', ?, 1, ?, 'pending')", (rid, steps[0]))
        flash(f"Refund needs approval: {' then '.join(label(s) for s in steps)}.")
    else:
        audit(g.db, None, "refund_auto_approved", "refund", rid, after={"status": "sent"},
              reason="Under auto-approve threshold")
        flash("Refund approved automatically and sent.")
    g.db.commit()
    return redirect(url_for("refund_detail", refund_id=rid))


@app.get("/refunds/<int:refund_id>")
def refund_detail(refund_id):
    require("refund_view")
    r = g.db.execute(
        "SELECT r.*, c.name AS customer, u.name AS requester, t.description, t.amount_cents AS txn_amount,"
        " t.created_at AS txn_at FROM refunds r JOIN customers c ON c.id = r.customer_id"
        " JOIN users u ON u.id = r.requested_by JOIN transactions t ON t.id = r.transaction_id WHERE r.id = ?",
        (refund_id,),
    ).fetchone()
    if not r:
        abort(404)
    approvals = g.db.execute(
        "SELECT a.*, u.name AS approver FROM approvals a LEFT JOIN users u ON u.id = a.decided_by"
        " WHERE entity_type = 'refund' AND entity_id = ? ORDER BY step",
        (refund_id,),
    ).fetchall()
    return render_template("refund.html", r=r, approvals=approvals, history=history("refund", refund_id))


@app.get("/approvals")
def approvals():
    require("approvals_view")
    return render_template("approvals.html", approvals=pending_approvals_for(g.user))


@app.post("/approvals/<int:approval_id>")
def approval_decide(approval_id):
    require("approvals_view")
    a = g.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    r = g.db.execute("SELECT * FROM refunds WHERE id = ?", (a["entity_id"],)).fetchone()
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()
    if a["status"] != "pending" or decision not in ("approved", "rejected"):
        abort(400)
    if g.user["role"] != "admin" and g.user["role"] != a["required_role"]:
        abort(403)
    if r["requested_by"] == g.user["id"]:
        flash("You can't approve a refund you requested.", "error")
        return redirect(url_for("approvals"))
    if decision == "rejected" and not comment:
        flash("Add a comment explaining the rejection.", "error")
        return redirect(url_for("approvals"))
    g.db.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, comment = ? WHERE id = ?",
                 (decision, g.user["id"], now_iso(), comment, approval_id))
    audit(g.db, g.user["id"], f"refund_step_{decision}", "refund", r["id"],
          after={"step": a["step"], "role": a["required_role"]}, reason=comment or None)
    lead_max = int(get_setting(g.db, "refund_lead_max_cents"))
    if decision == "rejected":
        g.db.execute("UPDATE refunds SET status = 'rejected', resolved_at = ? WHERE id = ?", (now_iso(), r["id"]))
    elif a["step"] == 1 and r["amount_cents"] > lead_max:
        g.db.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status)"
                     " VALUES ('refund', ?, 2, 'finance', 'pending')", (r["id"],))
    else:
        g.db.execute("UPDATE refunds SET status = 'sent', resolved_at = ? WHERE id = ?", (now_iso(), r["id"]))
        audit(g.db, None, "refund_sent", "refund", r["id"], after={"status": "sent"}, reason="Payout submitted (simulated)")
    g.db.commit()
    flash(f"Refund #{r['id']} {decision}.")
    return redirect(url_for("approvals"))


@app.get("/refunds/dashboard")
def refund_dashboard():
    require("refund_dashboard")
    db = g.db
    since = (datetime.now() - timedelta(days=84)).isoformat()
    rows = db.execute(
        "SELECT created_at, reason, amount_cents, requested_by, customer_id, status FROM refunds WHERE created_at > ?",
        (since,),
    ).fetchall()
    today = datetime.now().date()
    weeks = [(today - timedelta(days=7 * i + 6), today - timedelta(days=7 * i)) for i in reversed(range(12))]
    series = {reason: [0] * len(weeks) for reason in REFUND_REASONS}
    for row in rows:
        d = datetime.fromisoformat(row["created_at"]).date()
        for i, (start, end) in enumerate(weeks):
            if start <= d <= end:
                series[row["reason"]][i] += 1
    by_reason = db.execute(
        "SELECT reason, COUNT(*) n, SUM(amount_cents) total FROM refunds WHERE status = 'sent' AND created_at > ?"
        " GROUP BY reason ORDER BY n DESC",
        ((datetime.now() - timedelta(days=30)).isoformat(),),
    ).fetchall()
    by_agent = db.execute(
        "SELECT u.name, COUNT(*) n, SUM(r.amount_cents) total FROM refunds r JOIN users u ON u.id = r.requested_by"
        " WHERE r.created_at > ? GROUP BY u.name ORDER BY n DESC",
        ((datetime.now() - timedelta(days=30)).isoformat(),),
    ).fetchall()
    repeaters = db.execute(
        "SELECT c.id, c.name, COUNT(*) n, SUM(r.amount_cents) total FROM refunds r JOIN customers c ON c.id = r.customer_id"
        " WHERE r.created_at > ? GROUP BY c.id HAVING n >= 3 ORDER BY n DESC",
        ((datetime.now() - timedelta(days=30)).isoformat(),),
    ).fetchall()
    flag_changes = db.execute(
        "SELECT a.ts, a.reason, a.after_json, f.key, u.name FROM audit_log a JOIN feature_flags f ON f.id = a.entity_id"
        " LEFT JOIN users u ON u.id = a.actor_id WHERE a.entity_type = 'feature_flag' AND a.ts > ? ORDER BY a.ts DESC",
        (since,),
    ).fetchall()
    labels = [start.strftime("%b %d") for start, _ in weeks]
    return render_template("dashboard.html", labels=labels, series=series, by_reason=by_reason, by_agent=by_agent,
                           repeaters=repeaters, flag_changes=flag_changes)


# ---------- Feature flags ----------

@app.get("/flags")
def flags():
    require("flag_view")
    rows = g.db.execute(
        "SELECT f.*, u.name AS owner FROM feature_flags f JOIN users u ON u.id = f.owner_id ORDER BY f.critical DESC, f.key"
    ).fetchall()
    return render_template("flags.html", flags=rows)


@app.post("/flags/<int:flag_id>")
def flag_update(flag_id):
    require("flag_edit")
    f = g.db.execute("SELECT * FROM feature_flags WHERE id = ?", (flag_id,)).fetchone()
    if f["critical"] and not can("flag_edit_critical"):
        flash("Critical flags can only be changed by engineering.", "error")
        return redirect(url_for("flags"))
    enabled = 1 if request.form.get("enabled") == "on" else 0
    try:
        pct = max(0, min(100, int(request.form.get("rollout_pct", f["rollout_pct"]))))
    except ValueError:
        pct = f["rollout_pct"]
    reason = request.form.get("reason", "").strip()
    if f["critical"] and not reason:
        flash("Critical flags need a reason for every change.", "error")
        return redirect(url_for("flags"))
    before = {"enabled": f["enabled"], "rollout_pct": f["rollout_pct"]}
    after = {"enabled": enabled, "rollout_pct": pct}
    if before == after:
        return redirect(url_for("flags"))
    g.db.execute("UPDATE feature_flags SET enabled = ?, rollout_pct = ?, updated_at = ? WHERE id = ?",
                 (enabled, pct, now_iso(), flag_id))
    audit(g.db, g.user["id"], "flag_updated", "feature_flag", flag_id, before=before, after=after, reason=reason or None)
    g.db.commit()
    flash(f"{f['key']} updated.")
    return redirect(url_for("flags"))


# ---------- Audit & settings ----------

@app.get("/audit")
def audit_log():
    require("audit_view")
    entity_type = request.args.get("type", "")
    where = "WHERE entity_type = ?" if entity_type else ""
    rows = g.db.execute(
        f"SELECT a.*, u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id {where}"
        " ORDER BY ts DESC, a.id DESC LIMIT 300",
        (entity_type,) if entity_type else (),
    ).fetchall()
    return render_template("audit.html", rows=rows, entity_type=entity_type)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    rows = g.db.execute("SELECT * FROM settings").fetchall()
    if request.method == "POST":
        require("settings_edit")
        for row in rows:
            new = request.form.get(row["key"], "").strip()
            if new.isdigit() and new != row["value"]:
                g.db.execute("UPDATE settings SET value = ? WHERE key = ?", (new, row["key"]))
                audit(g.db, g.user["id"], "setting_updated", "setting", 0,
                      before={row["key"]: row["value"]}, after={row["key"]: new})
        g.db.commit()
        flash("Settings saved.")
        return redirect(url_for("settings"))
    return render_template("settings.html", rows=rows)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("403.html"), 403


if __name__ == "__main__":
    import os

    from db import DB_PATH

    if not os.path.exists(DB_PATH):
        seed.build()
    else:
        seed.migrate()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
