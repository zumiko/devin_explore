import difflib
import json
import os
import random
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

import ask
import devin_api
import entra
import face_match
import notify
import ocr
import seed
from db import audit, connect, get_setting, now_iso

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "demo-only-not-secret"
app.config.update(
    SESSION_COOKIE_NAME="acmepay_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PUBLIC_BASE_URL", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
if not entra.enabled():
    # Demo mode is often viewed inside an embedded preview frame, where Lax cookies are dropped.
    app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True, SESSION_COOKIE_PARTITIONED=True)
if entra.enabled() and app.secret_key == "demo-only-not-secret":
    raise SystemExit("Set SECRET_KEY when Microsoft sign-in is on.")
PUBLIC_ENDPOINTS = {"static", "signup", "resubmit", "login", "auth_callback", "logged_out"}
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

from access import PERMISSIONS, ROLES, LOCKED, setting_perm  # noqa: E402
KYC_REASONS = {
    "approve": ["false_positive_cleared", "docs_verified", "additional_docs_received"],
    "reject": ["identity_not_verified", "watchlist_confirmed", "fraud_suspected"],
    "request_info": ["need_clearer_id", "need_proof_of_address", "need_selfie_retake"],
    "escalate": ["complex_match", "possible_pep", "needs_senior_judgment"],
}
SOURCES_OF_FUNDS = ["salary", "business", "savings", "other"]
REFUND_REASONS = seed.REFUND_REASONS
SIMPLE_HOME_ROLES = {"kyc_analyst", "support_agent"}
APPS = [
    {"perm": "kyc_view", "endpoint": "kyc_list", "name": "KYC review queue", "desc": "Identity checks the automatic rules couldn't clear."},
    {"perm": "kyc_dashboard", "endpoint": "kyc_dashboard", "name": "KYC dashboard", "desc": "Signup trends and reviewer performance."},
    {"perm": "refund_view", "endpoint": "refunds", "name": "Refunds", "desc": "Look up charges and issue refunds."},
    {"perm": "approvals_view", "endpoint": "approvals", "name": "Refund approvals", "desc": "Refunds above the limit waiting on sign-off."},
    {"perm": "refund_dashboard", "endpoint": "refund_dashboard", "name": "Refund dashboard", "desc": "Refund volume by reason, agent and customer."},
    {"perm": "flag_view", "endpoint": "flags", "name": "Feature flags", "desc": "Turn features on, roll out gradually, target customers."},
    {"perm": "customer_view", "endpoint": "customers", "name": "Customers", "desc": "Customer profiles and history."},
    {"perm": "audit_view", "endpoint": "audit_log", "name": "Audit log", "desc": "Who changed what, when, and why."},
    {"perm": "settings_view", "endpoint": "settings", "name": "Rules", "desc": "Thresholds and limits the apps enforce."},
    {"perm": "change_request", "endpoint": "change_requests", "name": "Change requests",
     "desc": "Ask for a change to these tools. Admin approves, Devin builds it, an engineer reviews."},
    {"perm": "users_manage", "endpoint": "admin_access", "name": "Users & access", "desc": "Who has which role and what each role can do."},
    {"perm": "ask_data", "endpoint": "ask_data", "name": "Ask the data", "desc": "Questions in plain English, within your access."},
]
OPEN_KYC = ("new", "in_review", "needs_info", "escalated", "pending_second_review")


def can(perm):
    if g.get("user") is None:
        return False
    if "perms" not in g:
        g.perms = role_perms(g.user["role"]) - user_restrictions(g.user["id"])
    return perm in g.perms


def user_restrictions(user_id):
    return {r[0] for r in g.db.execute("SELECT perm FROM user_restrictions WHERE user_id = ?", (user_id,))}


def role_perms(role):
    return {r[0] for r in g.db.execute("SELECT perm FROM role_permissions WHERE role = ?", (role,))}


def require(perm):
    if not can(perm):
        abort(403)


@app.before_request
def load_user():
    g.db = connect()
    g.sso = entra.enabled()
    session.setdefault("csrf", secrets.token_urlsafe(24))
    if request.method == "POST" and request.endpoint not in PUBLIC_ENDPOINTS:
        if not secrets.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "Form expired or forged (CSRF check failed). Reload the page and try again.")
    uid = session.get("user_id", None if g.sso else 1)
    g.user = g.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
    g.users = [] if g.sso else g.db.execute("SELECT * FROM users ORDER BY id").fetchall()
    if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
        if g.sso:
            return redirect(url_for("login", next=request.full_path))
        abort(403)


@app.teardown_request
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject():
    pending = 0
    if g.get("user") is None:
        return {"can": can, "me": None, "all_users": [], "pending_count": 0, "sso": g.get("sso"), "csrf": session.get("csrf")}
    if can("approvals_view"):
        pending = len(pending_approvals_for(g.user))
    return {"can": can, "me": g.user, "all_users": g.users, "pending_count": pending, "sso": g.sso, "csrf": session["csrf"],
            "simple": g.user["role"] in SIMPLE_HOME_ROLES}


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
        "SELECT a.*, r.amount_cents, r.reason, r.note, r.requested_by, r.customer_id, r.created_at, r.risk_json,"
        " c.name AS customer,"
        " u.name AS requester FROM approvals a JOIN refunds r ON a.entity_type = 'refund' AND r.id = a.entity_id"
        " JOIN customers c ON c.id = r.customer_id JOIN users u ON u.id = r.requested_by"
        " WHERE a.status = 'pending' ORDER BY r.created_at"
    ).fetchall()
    return [r for r in rows if r["required_role"] == user["role"]]


def approval_context(rows):
    now = datetime.now()
    sla = int(get_setting(g.db, "refund_approval_sla_hours"))
    auto_max = int(get_setting(g.db, "refund_auto_approve_max_cents"))
    out = []
    for a in rows:
        since = g.db.execute("SELECT MAX(decided_at) FROM approvals WHERE entity_type = 'refund' AND entity_id = ?"
                             " AND step < ?", (a["entity_id"], a["step"])).fetchone()[0] or a["created_at"]
        cust = g.db.execute("SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM refunds WHERE customer_id = ?"
                            " AND id != ? AND status != 'rejected' AND created_at >= ?",
                            (a["customer_id"], a["entity_id"], (now - timedelta(days=90)).isoformat())).fetchone()
        agent = g.db.execute("SELECT COUNT(*), COALESCE(SUM(amount_cents), 0), COALESCE(SUM(amount_cents BETWEEN ? AND ?), 0)"
                             " FROM refunds WHERE requested_by = ? AND created_at >= ?",
                             (int(auto_max * 0.8), auto_max, a["requested_by"],
                              (now - timedelta(days=7)).isoformat())).fetchone()
        hours = (now - datetime.fromisoformat(since)).total_seconds() / 3600
        out.append(dict(a, risks=json.loads(a["risk_json"] or "[]"), waiting_hours=round(hours), overdue=hours > sla,
                        cust_count=cust[0], cust_sum=cust[1], agent_count=agent[0], agent_sum=agent[1],
                        agent_near_limit=agent[2]))
    return out


@app.post("/switch-user")
def switch_user():
    if g.sso:
        abort(403)
    session["user_id"] = int(request.form["user_id"])
    return redirect(url_for("index"))


@app.get("/login")
def login():
    if not g.sso:
        return redirect(url_for("index"))
    nxt = request.args.get("next", "/")
    session["next"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
    flow, url = entra.start_login(public_url("auth_callback"))
    session["auth_flow"] = flow
    return redirect(url)


@app.get("/auth/callback")
def auth_callback():
    if not g.sso:
        abort(404)
    try:
        ident = entra.finish_login(session.pop("auth_flow", {}), request.args)
    except (PermissionError, ValueError) as e:
        audit(g.db, None, "sign_in_failed", "user", 0, after={"error": str(e)[:200]})
        g.db.commit()
        abort(403, f"Microsoft sign-in failed: {e}")
    if not ident["role"]:
        audit(g.db, None, "sign_in_denied", "user", 0, after={"email": ident["email"], "reason": "no AcmePay group"})
        g.db.commit()
        abort(403, f"{ident['email']} signed in, but isn't in any AcmePay group in Entra. Ask IT for access.")
    local = ident["email"].split("@")[0].lower()
    row = g.db.execute("SELECT * FROM users WHERE lower(substr(email, 1, instr(email, '@') - 1)) = ?", (local,)).fetchone()
    if row:
        g.db.execute("UPDATE users SET role = ?, name = ? WHERE id = ?", (ident["role"], ident["name"] or row["name"], row["id"]))
        uid = row["id"]
    else:
        uid = g.db.execute("INSERT INTO users (name, email, role, team) VALUES (?, ?, ?, 'Entra')",
                           (ident["name"] or ident["email"], ident["email"], ident["role"])).lastrowid
    audit(g.db, uid, "sign_in", "user", uid, after={"via": "entra", "groups": ident["groups"], "role": ident["role"]})
    g.db.commit()
    nxt = session.pop("next", "/")
    session.clear()
    session.permanent = True
    session.update(user_id=uid, csrf=secrets.token_urlsafe(24))
    return redirect(nxt)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(entra.logout_url(public_url("logged_out")) if g.sso else url_for("index"))


@app.get("/logged-out")
def logged_out():
    return "<p>You're signed out. <a href='/login'>Sign in again</a></p>"


@app.post("/reset")
def reset():
    if g.sso:
        require("users_manage")
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
    my_refunds = db.execute(
        "SELECT r.*, c.name FROM refunds r JOIN customers c ON c.id = r.customer_id WHERE r.requested_by = ?"
        " ORDER BY r.created_at DESC LIMIT 8", (g.user["id"],)).fetchall() if can("refund_request") else []
    my_requests = db.execute("SELECT * FROM change_requests WHERE requester_id = ? AND status != 'done'"
                             " ORDER BY status = 'live' DESC, updated_at DESC", (g.user["id"],)).fetchall()
    waiting = (["submitted"] if can("change_approve") else []) + (
        ["approved", "building", "pr_open"] if can("change_ship") else [])
    review_requests = db.execute(
        f"SELECT * FROM change_requests WHERE status IN ({','.join('?' * len(waiting))}) ORDER BY updated_at",
        waiting).fetchall() if waiting else []
    my_flags = db.execute("SELECT * FROM feature_flags WHERE owner_id = ? ORDER BY key",
                          (g.user["id"],)).fetchall() if can("flag_view") else []
    unassigned = db.execute(
        "SELECT k.*, c.name FROM kyc_cases k JOIN customers c ON c.id = k.customer_id"
        " WHERE k.assigned_to IS NULL AND k.status = 'new' ORDER BY k.risk_score DESC, k.sla_due_at LIMIT 10"
    ).fetchall() if g.user["role"] in SIMPLE_HOME_ROLES and can("kyc_view") else []
    team = {}
    if can("kyc_team_view"):
        q = "SELECT k.*, c.name, u.name AS assignee, f.name AS first_decider FROM kyc_cases k JOIN customers c ON c.id = k.customer_id" \
            " LEFT JOIN users u ON u.id = k.assigned_to LEFT JOIN users f ON f.id = k.first_decision_by WHERE "
        team["signoff"] = db.execute(q + "k.status = 'pending_second_review' AND k.first_decision_by != ? ORDER BY k.sla_due_at",
                                     (g.user["id"],)).fetchall()
        team["escalated"] = db.execute(q + "k.status = 'escalated' ORDER BY k.sla_due_at").fetchall()
        team["overdue"] = db.execute(q + f"k.status IN {OPEN_KYC} AND k.sla_due_at < ? ORDER BY k.sla_due_at LIMIT 15",
                                     (now_iso(),)).fetchall()
        team["holding"] = db.execute(
            f"SELECT u.name, COUNT(*) AS open, SUM(k.sla_due_at < ?) AS overdue FROM kyc_cases k JOIN users u ON u.id = k.assigned_to"
            f" WHERE k.status IN {OPEN_KYC} GROUP BY u.id ORDER BY open DESC", (now_iso(),)).fetchall()
    return render_template("index.html", stats=stats, my_cases=my_cases, team=team, unassigned=unassigned, now=now_iso(), approvals=approvals, my_refunds=my_refunds,
                           my_flags=my_flags, my_requests=my_requests, review_requests=review_requests,
                           cr_labels=CR_STATUS, apps=[a for a in APPS if can(a["perm"])])


# ---------- KYC ----------

@app.get("/kyc")
def kyc_list():
    require("kyc_view")
    me = g.user["id"]
    if can("kyc_view_all"):
        views = ["open", "mine", "closed", "all"]
        filters = {
            "open": (f"k.status IN {OPEN_KYC}", []),
            "mine": (f"k.status IN {OPEN_KYC} AND k.assigned_to = ?", [me]),
            "closed": ("k.status IN ('approved', 'rejected')", []),
            "all": ("1", []),
        }
    else:
        views = ["mine", "unassigned", "closed"]
        filters = {
            "mine": (f"k.status IN {OPEN_KYC} AND k.assigned_to = ?", [me]),
            "unassigned": (f"k.status IN {OPEN_KYC} AND k.assigned_to IS NULL", []),
            "closed": ("k.status IN ('approved', 'rejected') AND ? IN (k.assigned_to, k.first_decision_by, k.decided_by)", [me]),
        }
    view = request.args.get("view", views[0])
    if view not in views:
        view = views[0]
    cond, params = filters[view]
    where = f"WHERE {cond}"
    cases = g.db.execute(
        "SELECT k.*, c.name, c.account_type, u.name AS assignee FROM kyc_cases k"
        " JOIN customers c ON c.id = k.customer_id LEFT JOIN users u ON u.id = k.assigned_to "
        f"{where} ORDER BY CASE WHEN k.status IN {OPEN_KYC} THEN 0 ELSE 1 END, k.risk_score DESC, k.sla_due_at",
        params,
    ).fetchall()
    return render_template("kyc_list.html", cases=cases, view=view, views=views, now=now_iso())


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
    if not can("kyc_view_all") and case["assigned_to"] not in (None, g.user["id"]) \
            and g.user["id"] not in (case["first_decision_by"], case["decided_by"]):
        abort(403, "This case is assigned to someone else.")
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
    messages = g.db.execute(
        "SELECT m.*, u.name AS sent_by_name FROM messages m LEFT JOIN users u ON u.id = m.sent_by"
        " WHERE m.case_id = ? ORDER BY m.id DESC", (case_id,)).fetchall()
    return render_template("kyc_case.html", case=case, customer=customer, flags=flags, messages=messages,
                           email_live=bool(notify.email_provider()), sms_live=bool(notify.sms_provider()),
                           reasons=KYC_REASONS, sources_of_funds=SOURCES_OF_FUNDS, history=history("kyc_case", case_id), face_checks=face_checks,
                           face_pass=int(get_setting(g.db, "face_match_pass_score")),
                           face_review=int(get_setting(g.db, "face_match_review_score")))


@app.post("/kyc/<int:case_id>/face-match")
def kyc_face_match(case_id):
    require("kyc_work")
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


def public_url(endpoint, **kw):
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or request.url_root.rstrip("/")
    return base + url_for(endpoint, **kw)


def customer_link(case_id, purpose, days=7):
    token = secrets.token_urlsafe(16)
    now = datetime.now().replace(microsecond=0)
    g.db.execute("INSERT INTO customer_links VALUES (?, ?, ?, ?, ?, NULL)",
                 (token, case_id, purpose, now.isoformat(), (now + timedelta(days=days)).isoformat()))
    return public_url("resubmit", token=token)


def save_face_check(case_id, run_by, source, face, id_bytes, id_mime, selfie_bytes, selfie_mime, id_read):
    details = {k: face[k] for k in ("id_faces_found", "selfie_faces_found", "id_face_confidence", "selfie_face_confidence")}
    details["model"] = "OpenCV YuNet + SFace (cosine similarity)"
    return g.db.execute(
        "INSERT INTO face_checks (case_id, run_by, source, created_at, score, verdict, id_image, id_image_type,"
        " selfie_image, selfie_image_type, id_face, selfie_face, details_json, ocr_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (case_id, run_by, source, now_iso(), face["score"], face["verdict"], id_bytes, id_mime or "image/jpeg",
         selfie_bytes, selfie_mime or "image/jpeg", face["id_face_jpg"], face["selfie_face_jpg"], json.dumps(details),
         json.dumps(id_read)),
    ).lastrowid


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
    form = {k: request.form.get(k, "").strip() for k in ("name", "email", "phone", "dob", "address", "state", "ssn_last4")}
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
        "INSERT INTO customers (name, email, phone, dob, address, state, ssn_last4, account_type, status, signup_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'personal', ?, ?)",
        (form["name"], form["email"], form["phone"], form["dob"], form["address"], form["state"].upper()[:2], form["ssn_last4"][-4:],
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
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    notify.send(g.db, case_id, customer, "signup_approved" if approved else "signup_review")
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


@app.post("/kyc/<int:case_id>/invite-call")
def kyc_invite_call(case_id):
    require("kyc_work")
    case = load_case(case_id)
    try:
        start = datetime.fromisoformat(f"{request.form['date']}T{request.form['time']}").replace(
            tzinfo=ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York")))
        minutes = int(request.form.get("minutes", 15))
    except (KeyError, ValueError):
        flash("Pick a date and time for the call.", "error")
        return redirect(url_for("kyc_case", case_id=case_id) + "#contact")
    link = f"https://meet.jit.si/AcmePay-KYC-{case_id}-{secrets.token_hex(4)}"
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone()
    notify.send(g.db, case_id, customer, "call_invite", sent_by=g.user["id"], ics=notify.call_invite_ics(start, minutes, link, case_id),
                link=link, when=start.strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " "), minutes=minutes,
                note=request.form.get("note", "").strip())
    audit(g.db, g.user["id"], "kyc_call_invited", "kyc_case", case_id, after={"when": start.isoformat(), "minutes": minutes, "link": link})
    g.db.commit()
    flash("Call invite sent to the customer.")
    return redirect(url_for("kyc_case", case_id=case_id) + "#contact")


@app.get("/kyc/message/<int:message_id>/invite.ics")
def message_ics(message_id):
    require("kyc_view")
    row = g.db.execute("SELECT ics FROM messages WHERE id = ? AND ics IS NOT NULL", (message_id,)).fetchone()
    if not row:
        abort(404)
    return Response(row["ics"], mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=invite.ics"})


RESUBMIT_ASKS = {
    "need_clearer_id": "We couldn't read your ID clearly. Please upload a new photo of it, plus a fresh selfie.",
    "need_selfie_retake": "We couldn't match your selfie to your ID. Please take a new selfie and upload it with your ID.",
    "need_proof_of_address": "Please upload your ID and a selfie again; our team will also email you about proof of address.",
}


@app.route("/resubmit/<token>", methods=["GET", "POST"])
def resubmit(token):
    """Customer-facing page behind the link in a 'we need more info' email or text."""
    link = g.db.execute("SELECT * FROM customer_links WHERE token = ?", (token,)).fetchone()
    if not link or link["used_at"] or link["expires_at"] < now_iso():
        return render_template("resubmit.html", state="invalid")
    case = load_case(link["case_id"])
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone()
    ask = RESUBMIT_ASKS.get(link["purpose"], "Please upload a new photo of your ID and a selfie.")
    if request.method == "GET":
        return render_template("resubmit.html", state="form", name=customer["name"], ask=ask)
    id_file, selfie_file = request.files.get("id_image"), request.files.get("selfie")
    if not id_file or not id_file.filename or not selfie_file or not selfie_file.filename:
        flash("Please add both photos.", "error")
        return render_template("resubmit.html", state="form", name=customer["name"], ask=ask)
    id_bytes, selfie_bytes = id_file.read(), selfie_file.read()
    try:
        face = face_match.compare(id_bytes, selfie_bytes)
    except face_match.FaceMatchError as e:
        flash(f"{e} Please try again with a clearer photo.", "error")
        return render_template("resubmit.html", state="form", name=customer["name"], ask=ask)
    pct = max(0, round(face["score"] * 100))
    face["verdict"] = face_match.verdict(pct, int(get_setting(g.db, "face_match_pass_score")),
                                         int(get_setting(g.db, "face_match_review_score")))
    id_read = ocr.check(id_bytes, customer, int(get_setting(g.db, "ocr_name_match_pct")))
    fc_id = save_face_check(case["id"], None, "customer_resubmit", face, id_bytes, id_file.mimetype,
                            selfie_bytes, selfie_file.mimetype, id_read)
    g.db.execute("UPDATE customer_links SET used_at = ? WHERE token = ?", (now_iso(), token))
    new_status = "in_review" if case["status"] == "needs_info" else case["status"]
    g.db.execute("UPDATE kyc_cases SET status = ? WHERE id = ?", (new_status, case["id"]))
    audit(g.db, None, "customer_resubmitted", "kyc_case", case["id"], before={"status": case["status"]},
          after={"status": new_status, "face_check_id": fc_id, "score": pct, "verdict": face["verdict"]},
          reason="Customer uploaded new photos from the link we sent")
    g.db.commit()
    return render_template("resubmit.html", state="done", name=customer["name"], case_id=case["id"])


@app.post("/kyc/<int:case_id>/claim")
def kyc_claim(case_id):
    require("kyc_work")
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


@app.post("/kyc/<int:case_id>/source-of-funds")
def kyc_source_of_funds(case_id):
    require("kyc_work")
    case = load_case(case_id)
    source = request.form.get("source_of_funds", "")
    note = request.form.get("source_of_funds_note", "").strip()
    if not case["requires_second_review"]:
        flash("Source of funds is only recorded on high-risk cases.", "error")
    elif case["status"] in ("approved", "rejected"):
        flash("This case is already closed.", "error")
    elif case["assigned_to"] != g.user["id"] and not (case["status"] == "pending_second_review" and can("kyc_second_review")):
        flash("Claim the case before recording the source of funds.", "error")
    elif source not in SOURCES_OF_FUNDS or not note:
        flash("Pick where the money comes from and write a note.", "error")
    else:
        g.db.execute("UPDATE kyc_cases SET source_of_funds = ?, source_of_funds_note = ? WHERE id = ?",
                     (source, note, case_id))
        audit(g.db, g.user["id"], "kyc_source_of_funds_recorded", "kyc_case", case_id,
              before={"source_of_funds": case["source_of_funds"], "note": case["source_of_funds_note"]}
              if case["source_of_funds"] else None,
              after={"source_of_funds": source, "note": note})
        g.db.commit()
        flash("Source of funds saved.")
    return redirect(url_for("kyc_case", case_id=case_id) + "#source-of-funds")


@app.post("/kyc/<int:case_id>/decide")
def kyc_decide(case_id):
    require("kyc_work")
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

    if decision == "approve" and case["requires_second_review"] and not case["source_of_funds"]:
        flash("High-risk case: record the source of funds before approving.", "error")
        return redirect(url_for("kyc_case", case_id=case_id) + "#source-of-funds")

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
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone()
    if decision == "request_info":
        notify.send(g.db, case_id, customer, reason, sent_by=g.user["id"], link=customer_link(case_id, reason),
                    note=request.form.get("customer_note", "").strip())
    elif updates["status"] in ("approved", "rejected"):
        notify.send(g.db, case_id, customer, updates["status"], sent_by=g.user["id"])
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


def agent_auto_today(user_id):
    return g.db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds r WHERE requested_by = ? AND created_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.entity_type = 'refund' AND a.entity_id = r.id)",
        (user_id, datetime.now().strftime("%Y-%m-%dT00:00:00"))).fetchone()[0]


def refund_risks(txn, amount, auto_max):
    """Reasons a refund needs a lead even though it's under the auto-approve limit."""
    s = {k: int(get_setting(g.db, k)) for k in (
        "refund_repeat_max", "refund_max_age_days", "refund_agent_daily_max_cents")}
    now = datetime.now()
    risks = []
    recent = g.db.execute("SELECT COUNT(*) FROM refunds WHERE customer_id = ? AND status != 'rejected'"
                          " AND created_at >= ?", (txn["customer_id"], (now - timedelta(days=30)).isoformat())).fetchone()[0]
    if recent >= s["refund_repeat_max"]:
        risks.append(f"customer's refund #{recent + 1} in 30 days")
    mine = g.db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds WHERE transaction_id = ? AND requested_by = ?"
                        " AND status != 'rejected' AND created_at >= ?",
                        (txn["id"], g.user["id"], (now - timedelta(hours=24)).isoformat())).fetchone()[0]
    if mine and mine + amount > auto_max:
        risks.append(f"together with your {money(mine)} on this charge today it's over the {money(auto_max)} auto-approve limit")
    kyc = g.db.execute("SELECT status FROM kyc_cases WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
                       (txn["customer_id"],)).fetchone()
    if kyc and kyc["status"] != "approved":
        risks.append(f"customer's identity check is {label(kyc['status']).lower()}")
    age = (now - datetime.fromisoformat(txn["created_at"])).days
    if age > s["refund_max_age_days"]:
        risks.append(f"charge is {age} days old")
    today = agent_auto_today(g.user["id"])
    if amount <= auto_max and today + amount > s["refund_agent_daily_max_cents"]:
        risks.append(f"over your {money(s['refund_agent_daily_max_cents'])} daily limit ({money(today)} used)")
    return risks


def notify_refund(refund_id, template):
    r = g.db.execute("SELECT r.amount_cents, r.customer_id, t.description FROM refunds r"
                     " JOIN transactions t ON t.id = r.transaction_id WHERE r.id = ?", (refund_id,)).fetchone()
    customer = g.db.execute("SELECT * FROM customers WHERE id = ?", (r["customer_id"],)).fetchone()
    notify.send(g.db, None, customer, template, sent_by=g.get("user") and g.user["id"],
                amount=money(r["amount_cents"]), description=r["description"])


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
                               auto_max=auto_max, lead_max=lead_max, used_today=agent_auto_today(g.user["id"]),
                               daily_max=int(get_setting(g.db, "refund_agent_daily_max_cents")))

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
    risks = refund_risks(txn, amount, auto_max)
    if risks and not steps:
        steps = ["support_lead"]
    status = "sent" if not steps else "pending_approval"
    cur = g.db.execute(
        "INSERT INTO refunds (transaction_id, customer_id, amount_cents, reason, note, status, requested_by,"
        " created_at, resolved_at, risk_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (txn_id, txn["customer_id"], amount, reason, note, status, g.user["id"], now_iso(),
         now_iso() if status == "sent" else None, json.dumps(risks) if risks else None),
    )
    rid = cur.lastrowid
    audit(g.db, g.user["id"], "refund_requested", "refund", rid, after={"amount_cents": amount, "reason": reason}, reason=note)
    if steps:
        g.db.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status)"
                     " VALUES ('refund', ?, 1, ?, 'pending')", (rid, steps[0]))
        flash(f"Refund needs approval: {' then '.join(label(s) for s in steps)}."
              + (f" Why: {'; '.join(risks)}." if risks else ""))
    else:
        audit(g.db, None, "refund_auto_approved", "refund", rid, after={"status": "sent"},
              reason="Under auto-approve threshold")
        notify_refund(rid, "refund_sent")
        flash("Refund approved automatically and sent. The customer has been emailed.")
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
    messages = g.db.execute("SELECT * FROM messages WHERE customer_id = ? AND case_id IS NULL AND created_at >= ?"
                            " AND (subject LIKE '%refund%' OR body LIKE '%refund%') ORDER BY id",
                            (r["customer_id"], r["created_at"])).fetchall()
    return render_template("refund.html", r=r, approvals=approvals, history=history("refund", refund_id),
                           messages=messages)


@app.get("/approvals")
def approvals():
    require("approvals_view")
    return render_template("approvals.html", approvals=approval_context(pending_approvals_for(g.user)),
                           sla=int(get_setting(g.db, "refund_approval_sla_hours")), auto_max=int(get_setting(
                               g.db, "refund_auto_approve_max_cents")))


@app.get("/refunds/export.csv")
def refunds_export():
    require("refund_export")
    import csv
    import io
    start, end = request.args.get("from", ""), request.args.get("to", "")
    rows = g.db.execute(
        "SELECT r.id, r.resolved_at, r.created_at, c.id AS customer_id, c.name, r.transaction_id, r.amount_cents,"
        " r.reason, u.name AS agent FROM refunds r JOIN customers c ON c.id = r.customer_id JOIN users u"
        " ON u.id = r.requested_by WHERE r.status = 'sent' AND (? = '' OR r.resolved_at >= ?)"
        " AND (? = '' OR r.resolved_at < date(?, '+1 day')) ORDER BY r.resolved_at", (start, start, end, end)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["refund_id", "sent_at", "requested_at", "customer_id", "customer", "transaction_id", "amount",
                "reason", "requested_by", "approved_by"])
    for r in rows:
        approvers = ", ".join(x[0] for x in g.db.execute(
            "SELECT u.name FROM approvals a JOIN users u ON u.id = a.decided_by WHERE a.entity_type = 'refund'"
            " AND a.entity_id = ? AND a.status = 'approved' ORDER BY step", (r["id"],))) or "auto"
        w.writerow([r["id"], r["resolved_at"], r["created_at"], r["customer_id"], r["name"], r["transaction_id"],
                    f"{r['amount_cents'] / 100:.2f}", r["reason"], r["agent"], approvers])
    audit(g.db, g.user["id"], "refunds_exported", "refund", 0, after={"rows": len(rows), "from": start, "to": end})
    g.db.commit()
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=refunds_sent.csv"})


@app.post("/approvals/<int:approval_id>")
def approval_decide(approval_id):
    require("approvals_view")
    a = g.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    r = g.db.execute("SELECT * FROM refunds WHERE id = ?", (a["entity_id"],)).fetchone()
    decision = request.form.get("decision")
    comment = request.form.get("comment", "").strip()
    if a["status"] != "pending" or decision not in ("approved", "rejected"):
        abort(400)
    if g.user["role"] != a["required_role"]:
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
        notify_refund(r["id"], "refund_rejected")
    elif a["step"] == 1 and r["amount_cents"] > lead_max:
        g.db.execute("INSERT INTO approvals (entity_type, entity_id, step, required_role, status)"
                     " VALUES ('refund', ?, 2, 'finance', 'pending')", (r["id"],))
    else:
        g.db.execute("UPDATE refunds SET status = 'sent', resolved_at = ? WHERE id = ?", (now_iso(), r["id"]))
        audit(g.db, None, "refund_sent", "refund", r["id"], after={"status": "sent"}, reason="Payout submitted (simulated)")
        notify_refund(r["id"], "refund_sent")
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


@app.get("/kyc/dashboard")
def kyc_dashboard():
    require("kyc_dashboard")
    weeks_back = 12
    today = datetime.now().date()
    weeks = [(today - timedelta(days=7 * i + 6), today - timedelta(days=7 * i)) for i in reversed(range(weeks_back))]
    since = datetime.combine(weeks[0][0], datetime.min.time()).isoformat()
    cases = g.db.execute(
        "SELECT k.*, u.name AS reviewer FROM kyc_cases k LEFT JOIN users u ON u.id = k.decided_by WHERE k.created_at >= ?",
        (since,)).fetchall()

    def week_of(ts):
        d = datetime.fromisoformat(ts).date()
        return next((i for i, (a, b) in enumerate(weeks) if a <= d <= b), None)

    def hours(c):
        return (datetime.fromisoformat(c["decided_at"]) - datetime.fromisoformat(c["created_at"])).total_seconds() / 3600

    def median(xs):
        xs = sorted(xs)
        return round((xs[len(xs) // 2] + xs[(len(xs) - 1) // 2]) / 2, 1) if xs else None

    n = len(weeks)
    volume = {k: [0] * n for k in ("auto_approved", "approved_by_reviewer", "rejected", "open")}
    review_hours, breaches, manual = [[] for _ in range(n)], [0] * n, [0] * n
    per_reviewer = {}
    for c in cases:
        w = week_of(c["created_at"])
        if w is None:
            continue
        if c["status"] not in ("approved", "rejected"):
            volume["open"][w] += 1
            continue
        if c["decision_reason"] == "auto_approved":
            volume["auto_approved"][w] += 1
            continue
        volume["approved_by_reviewer" if c["status"] == "approved" else "rejected"][w] += 1
        h = hours(c)
        review_hours[w].append(h)
        manual[w] += 1
        breaches[w] += c["decided_at"] > c["sla_due_at"]
        r = per_reviewer.setdefault(c["reviewer"] or "Unknown", {"hours": [], "approved": 0, "late": 0, "weekly": [0] * n, "second": 0})
        r["hours"].append(h)
        r["approved"] += c["status"] == "approved"
        r["late"] += c["decided_at"] > c["sla_due_at"]
        r["weekly"][w] += 1
        r["second"] += bool(c["first_decision_by"])
    open_by = {row["name"]: row for row in g.db.execute(
        f"SELECT u.name, COUNT(*) n, MIN(k.created_at) oldest FROM kyc_cases k JOIN users u ON u.id = k.assigned_to"
        f" WHERE k.status IN {OPEN_KYC} GROUP BY u.name").fetchall()}
    reviewers = []
    for name, r in sorted(per_reviewer.items(), key=lambda kv: -len(kv[1]["hours"])):
        total = len(r["hours"])
        reviewers.append({"name": name, "decided": total, "approved_pct": round(100 * r["approved"] / total),
                          "median_hours": median(r["hours"]), "on_time_pct": round(100 * (total - r["late"]) / total),
                          "second_reviews": r["second"], "weekly": r["weekly"],
                          "open_now": open_by[name]["n"] if name in open_by else 0,
                          "oldest_open": open_by[name]["oldest"] if name in open_by else None})
    total_decided = sum(volume["auto_approved"]) + sum(manual)
    summary = {
        "signups": len(cases),
        "auto_pct": round(100 * sum(volume["auto_approved"]) / total_decided) if total_decided else 0,
        "median_hours": median([h for ws in review_hours for h in ws]),
        "on_time_pct": round(100 * (sum(manual) - sum(breaches)) / sum(manual)) if sum(manual) else None,
        "open": g.db.execute(f"SELECT COUNT(*) FROM kyc_cases WHERE status IN {OPEN_KYC}").fetchone()[0],
    }
    return render_template(
        "kyc_dashboard.html", labels=[a.strftime("%b %d") for a, _ in weeks], volume=volume,
        median_hours=[median(x) for x in review_hours],
        breach_pct=[round(100 * b / m) if m else 0 for b, m in zip(breaches, manual)],
        reviewers=reviewers, summary=summary)


@app.route("/ask", methods=["GET", "POST"])
def ask_data():
    require("ask_data")
    tables = ask.allowed_tables(can)
    question, sql, result, error = request.form.get("question", "").strip(), request.form.get("sql", "").strip(), None, None
    if request.method == "POST":
        try:
            if request.form.get("mode") == "ask":
                if not question:
                    raise ask.QueryError("Type a question first.")
                sql = ask.to_sql(question, ask.schema_text(g.db, tables))
            result = ask.run(sql, tables)
        except ask.QueryError as e:
            error = str(e)
        except Exception as e:
            error = f"The AI service call failed: {e}"
        audit(g.db, g.user["id"], "data_query", "query", 0,
              after={"question": question or None, "sql": sql, "rows": len(result["rows"]) if result else None,
                     "masked": result["masked"] if result else None, "error": error})
        g.db.commit()
    return render_template("ask.html", question=question, sql=sql, result=result, error=error,
                           tables=sorted(tables), masked=ask.MASKED, ai=ask.provider())


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
    entity_type = request.args.get("type", "") if can("audit_view_all") else "feature_flag"
    where = "WHERE entity_type = ?" if entity_type else ""
    rows = g.db.execute(
        f"SELECT a.*, u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id {where}"
        " ORDER BY ts DESC, a.id DESC LIMIT 300",
        (entity_type,) if entity_type else (),
    ).fetchall()
    return render_template("audit.html", rows=rows, entity_type=entity_type)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    require("settings_view")
    rows = g.db.execute("SELECT * FROM settings").fetchall()
    if request.method == "POST":
        editable = [row for row in rows if can(setting_perm(row["key"]))]
        if not editable:
            abort(403)
        for row in editable:
            new = request.form.get(row["key"], "").strip()
            if new.isdigit() and new != row["value"]:
                g.db.execute("UPDATE settings SET value = ? WHERE key = ?", (new, row["key"]))
                audit(g.db, g.user["id"], "setting_updated", "setting", 0,
                      before={row["key"]: row["value"]}, after={row["key"]: new})
        g.db.commit()
        flash("Settings saved.")
        return redirect(url_for("settings"))
    return render_template("settings.html", rows=rows, editable={r["key"] for r in rows if can(setting_perm(r["key"]))})


# ---------- Admin: people and access ----------

def entra_group(role):
    return next((grp for grp, r in entra.GROUP_ROLES.items() if r == role), "")


@app.get("/admin/access")
def admin_access():
    require("users_manage")
    granted = {}
    for role, perm in g.db.execute("SELECT role, perm FROM role_permissions"):
        granted.setdefault(role, set()).add(perm)
    users = g.db.execute("SELECT * FROM users ORDER BY id").fetchall()
    restricted = {}
    for uid, perm in g.db.execute("SELECT user_id, perm FROM user_restrictions"):
        restricted.setdefault(uid, set()).add(perm)

    def apps(perms):
        return [a["name"] for a in APPS if a["perm"] in perms]
    return render_template("admin_access.html", users=users, roles=ROLES, perms=PERMISSIONS, granted=granted,
                           restricted=restricted, apps=apps, group=entra_group, locked=LOCKED)


CR_STATUS = {
    "submitted": "Waiting for admin approval",
    "declined": "Declined",
    "approved": "Approved, Devin not connected",
    "building": "Devin is building it",
    "pr_open": "Code ready, waiting for engineer review",
    "live": "Live, waiting for requester to confirm",
    "done": "Done",
}


def load_request(req_id):
    req = g.db.execute("SELECT c.*, u.name AS requester, u.role AS requester_role, u.team AS requester_team,"
                       " a.name AS approver FROM change_requests c JOIN users u ON u.id = c.requester_id"
                       " LEFT JOIN users a ON a.id = c.approver_id WHERE c.id = ?", (req_id,)).fetchone()
    if not req:
        abort(404)
    if req["requester_id"] != g.user["id"] and not (can("change_approve") or can("change_ship")):
        abort(403)
    return req


def set_request(req, status, action, reason=None, **fields):
    fields.update(status=status, updated_at=now_iso())
    g.db.execute(f"UPDATE change_requests SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                 (*fields.values(), req["id"]))
    audit(g.db, g.user["id"], action, "change_request", req["id"], before={"status": req["status"]},
          after={k: v for k, v in fields.items() if k not in ("prompt", "updated_at")}, reason=reason)
    g.db.commit()


@app.route("/requests")
def change_requests():
    require("change_request")
    everyone = can("change_approve") or can("change_ship")
    rows = g.db.execute("SELECT c.*, u.name AS requester FROM change_requests c JOIN users u ON u.id = c.requester_id"
                        + ("" if everyone else " WHERE c.requester_id = ?") + " ORDER BY c.updated_at DESC",
                        () if everyone else (g.user["id"],)).fetchall()
    return render_template("change_requests.html", rows=rows, everyone=everyone, labels=CR_STATUS)


@app.route("/requests/new", methods=["GET", "POST"])
def change_request_new():
    require("change_request")
    if request.method == "POST":
        title, details = request.form.get("title", "").strip(), request.form.get("details", "").strip()
        if not title or not details:
            flash("Give the request a title and describe what you need.", "error")
            return render_template("change_request_new.html", form=request.form)
        ts = now_iso()
        cur = g.db.execute("INSERT INTO change_requests (requester_id, title, details, created_at, updated_at)"
                           " VALUES (?, ?, ?, ?, ?)", (g.user["id"], title, details, ts, ts))
        audit(g.db, g.user["id"], "change_requested", "change_request", cur.lastrowid, after={"title": title})
        g.db.commit()
        flash("Sent to the admin for approval.")
        return redirect(url_for("change_request", req_id=cur.lastrowid))
    return render_template("change_request_new.html", form={})


@app.route("/requests/<int:req_id>", methods=["GET", "POST"])
def change_request(req_id):
    require("change_request")
    req = load_request(req_id)
    if request.method == "POST":
        action, note = request.form.get("action"), request.form.get("note", "").strip()
        mine = req["requester_id"] == g.user["id"]
        if action in ("approve", "decline") and can("change_approve") and req["status"] == "submitted":
            if action == "decline":
                set_request(req, "declined", "change_declined", note, approver_id=g.user["id"], approver_note=note)
            else:
                requester = g.db.execute("SELECT * FROM users WHERE id = ?", (req["requester_id"],)).fetchone()
                prompt = devin_api.prompt_for(req, requester, note)
                fields = dict(approver_id=g.user["id"], approver_note=note, prompt=prompt)
                if devin_api.configured():
                    try:
                        s = devin_api.start(prompt, f"Change request #{req['id']}: {req['title']}")
                    except Exception as e:
                        flash(f"Couldn't start Devin: {e}", "error")
                        return redirect(url_for("change_request", req_id=req_id))
                    set_request(req, "building", "change_approved", note, devin_session_id=s["session_id"],
                                devin_url=s["url"], **fields)
                    flash("Approved. Devin is building it.")
                else:
                    set_request(req, "approved", "change_approved", note, **fields)
                    flash("Approved. Devin isn't connected, so the request it would get is shown below.")
        elif action == "refresh" and req["devin_session_id"] and req["status"] in ("building", "pr_open"):
            try:
                s = devin_api.status(req["devin_session_id"])
            except Exception as e:
                flash(f"Couldn't reach Devin: {e}", "error")
                return redirect(url_for("change_request", req_id=req_id))
            if s["pr_url"] and s["pr_url"] != req["pr_url"]:
                set_request(req, "pr_open", "change_pr_opened", pr_url=s["pr_url"])
            flash(f"Devin session: {s['status'] or 'unknown'}." + ("" if s["pr_url"] else " No PR yet."))
        elif action == "live" and can("change_ship") and req["status"] in ("approved", "building", "pr_open"):
            set_request(req, "live", "change_live", note, **({"pr_url": request.form["pr_url"].strip()}
                                                             if request.form.get("pr_url", "").strip() else {}))
            flash(f"Marked live. {req['requester'].split()[0]} will see it on their home page to confirm.")
        elif action in ("confirm", "reopen") and mine and req["status"] == "live":
            if action == "confirm":
                set_request(req, "done", "change_confirmed", note)
                flash("Thanks, closed.")
            else:
                set_request(req, "submitted", "change_reopened", note,
                            details=req["details"] + f"\n\nReopened by {g.user['name']}: {note or '(no reason given)'}")
                flash("Reopened and sent back to the admin.")
        else:
            abort(403)
        return redirect(url_for("change_request", req_id=req_id))
    history = g.db.execute("SELECT a.*, u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id"
                           " WHERE entity_type = 'change_request' AND entity_id = ? ORDER BY ts, a.id",
                           (req_id,)).fetchall()
    return render_template("change_request.html", req=req, labels=CR_STATUS, history=history,
                           devin_on=devin_api.configured(), repo=devin_api.REPO)


@app.route("/admin/users/<int:user_id>", methods=["GET", "POST"])
def admin_user(user_id):
    require("users_manage")
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)
    group_perms = [p for p in PERMISSIONS if p in role_perms(user["role"])]
    before = user_restrictions(user_id)
    if request.method == "POST":
        if user_id == g.user["id"]:
            flash("You can't restrict yourself. Ask another admin.", "error")
            return redirect(url_for("admin_user", user_id=user_id))
        after = {p for p in group_perms if not request.form.get(p)}
        if after != before:
            g.db.execute("DELETE FROM user_restrictions WHERE user_id = ?", (user_id,))
            g.db.executemany("INSERT INTO user_restrictions VALUES (?, ?)", [(user_id, p) for p in sorted(after)])
            audit(g.db, g.user["id"], "user_restrictions_changed", "user", user_id,
                  before={"name": user["name"], "restricted": sorted(before)},
                  after={"name": user["name"], "restricted": sorted(after)}, reason=request.form.get("reason", "").strip())
            g.db.commit()
            flash(f"Saved restrictions for {user['name']}.")
        return redirect(url_for("admin_access"))
    return render_template("admin_user.html", user=user, group=entra_group(user["role"]), group_perms=group_perms,
                           perms=PERMISSIONS, restricted=before)


@app.post("/admin/users/<int:user_id>/role")
def admin_set_role(user_id):
    require("users_manage")
    if g.sso:
        abort(403, "With Microsoft sign-in on, roles come from Entra groups. Change the person's group in Entra.")
    role = request.form.get("role")
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or role not in dict((r[0], r) for r in ROLES):
        abort(400)
    if user_id == g.user["id"] and role != user["role"]:
        flash("You can't change your own role. Ask another admin.", "error")
    elif role != user["role"]:
        g.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        audit(g.db, g.user["id"], "user_role_changed", "user", user_id,
              before={"name": user["name"], "role": user["role"]}, after={"name": user["name"], "role": role})
        g.db.commit()
        flash(f"{user['name']} is now {role.replace('_', ' ')} (Entra group {entra_group(role)}).")
    return redirect(url_for("admin_access"))


@app.post("/admin/permissions")
def admin_set_permissions():
    require("users_manage")
    before = {(r, p) for r, p in g.db.execute("SELECT role, perm FROM role_permissions")}
    after = {(r, p) for r, _, _ in ROLES for p in PERMISSIONS if request.form.get(f"{r}:{p}")} | LOCKED
    added, removed = sorted(after - before), sorted(before - after)
    if added or removed:
        g.db.execute("DELETE FROM role_permissions")
        g.db.executemany("INSERT INTO role_permissions VALUES (?, ?)", sorted(after))
        for role in {r for r, _ in added + removed}:
            audit(g.db, g.user["id"], "role_permissions_changed", "role", 0,
                  before={"role": role, "removed": [p for r, p in removed if r == role]},
                  after={"role": role, "added": [p for r, p in added if r == role]})
        g.db.commit()
        flash(f"Saved: {len(added)} permission(s) added, {len(removed)} removed.")
    return redirect(url_for("admin_access"))


@app.errorhandler(403)
def forbidden(_e):
    if request.method == "GET" and g.get("user") is not None and request.endpoint != "index":
        flash(f"That page isn't one of {g.user['name'].split()[0]}'s tools, so here's their home page.")
        return redirect(url_for("index"))
    return render_template("403.html"), 403


if __name__ == "__main__":
    from db import DB_PATH

    if not os.path.exists(DB_PATH):
        seed.build()
    else:
        seed.migrate()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
