"""Ask the data in plain English.

An AI model turns the question into one SQLite SELECT. The query then runs on a read-only connection with an
authorizer that only allows reading the tables the person's role may see, blanks personal data columns, and
rejects anything that isn't a plain read. The guard does not trust the model: hand-written SQL gets the same checks.
"""
import json
import os
import re
import sqlite3
import urllib.request
from datetime import date

from db import DB_PATH

MAX_ROWS = 200
MASKED = {
    "customers": {"email", "dob", "address", "ssn_last4", "phone"},
    "messages": {"to_addr", "body", "ics"},
    "face_checks": {"id_image", "selfie_image", "id_face", "selfie_face"},
    "users": {"email"},
}
# table -> permission needed (see access.py)
TABLES = {
    "users": "ask_data",
    "feature_flags": "flag_view",
    "settings": "settings_view",
    "customers": "customer_view",
    "kyc_cases": "kyc_view",
    "watchlist": "kyc_view",
    "face_checks": "kyc_view",
    "messages": "kyc_view",
    "transactions": "refund_view",
    "refunds": "refund_view",
    "approvals": "approvals_view",
    "audit_log": "audit_view_all",
}
NOTES = """Notes:
- Timestamps are ISO-8601 text in local time, e.g. '2026-11-14T09:30:00'. Today is {today}.
- kyc_cases.status: new, in_review, needs_info, escalated, pending_second_review, approved, rejected.
- kyc_cases.decision_reason = 'auto_approved' means approved by automatic rules with no reviewer (decided_by is NULL).
- kyc_cases.decided_by, assigned_to, first_decision_by and refunds.requested_by reference users.id; users.name is the person.
- A case is late when decided_at > sla_due_at (or still open and now is past sla_due_at).
- Money columns ending in _cents are integer cents.
- face_checks.score is 0-1; verdict is likely_same_person, reviewer_decides or likely_different_people."""


class QueryError(Exception):
    pass


def allowed_tables(can):
    return {t for t, perm in TABLES.items() if can(perm)}


def schema_text(conn, tables):
    out = []
    for t in sorted(tables):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        hidden = MASKED.get(t, set())
        out.append(f"{t}({', '.join(c for c in cols if c not in hidden)})")
    return "\n".join(out) + "\n\n" + NOTES.format(today=date.today().isoformat())


def run(sql, tables):
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise QueryError("Only SELECT queries can run here.")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    masked_hits = set()

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if arg2 in ("load_extension", "readfile", "writefile") else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 not in tables:
                return sqlite3.SQLITE_DENY
            if arg2 in MASKED.get(arg1, ()):
                masked_hits.add(f"{arg1}.{arg2}")
                return sqlite3.SQLITE_IGNORE
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    steps = [0]

    def budget():
        steps[0] += 1
        return 1 if steps[0] > 2000 else 0  # ~20M VM instructions, then abort

    conn.set_authorizer(authorizer)
    conn.set_progress_handler(budget, 10000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except (sqlite3.Error, sqlite3.Warning) as e:
        msg = str(e)
        if "not authorized" in msg or "prohibited" in msg or "authorization denied" in msg:
            msg = "Not allowed: the query reads a table your role can't see, or isn't a plain read."
        elif "interrupted" in msg:
            msg = "The query took too long and was stopped."
        raise QueryError(msg)
    finally:
        conn.close()
    return {"columns": cols, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS,
            "masked": sorted(masked_hits)}


def provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def to_sql(question, schema):
    system = ("You write one SQLite SELECT query that answers the user's question about an internal fintech database. "
              "Use only these tables and columns; other tables and personal data are not available to this user.\n\n"
              + schema + "\n\nReply with only the SQL, no explanation and no code fences. Use readable column aliases. "
              "If the question can't be answered from these tables, reply with: SELECT 'Not answerable from data you can access' AS note")
    p = provider()
    if p == "anthropic":
        data = _post("https://api.anthropic.com/v1/messages",
                     {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                     {"model": os.environ.get("ASK_MODEL", "claude-sonnet-4-5"), "max_tokens": 800, "system": system,
                      "messages": [{"role": "user", "content": question}]})
        text = "".join(b.get("text", "") for b in data["content"])
    elif p == "openai":
        data = _post("https://api.openai.com/v1/chat/completions",
                     {"authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                     {"model": os.environ.get("ASK_MODEL", "gpt-4o-mini"),
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}]})
        text = data["choices"][0]["message"]["content"]
    else:
        raise QueryError("No AI provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or write the SQL yourself below.")
    return re.sub(r"^```(?:sql)?|```$", "", text.strip(), flags=re.I).strip()
