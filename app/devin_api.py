"""Starts a Devin session for an approved change request and reads back its status and PR.

Off unless DEVIN_API_KEY is set; without it the app records the request it would have sent.
"""
import json
import os
import urllib.request

API = "https://api.devin.ai"
ORG_ID = os.environ.get("DEVIN_ORG_ID", "")
REPO = os.environ.get("CHANGE_REPO", "zumiko/devin_explore")
BASE_BRANCH = os.environ.get("CHANGE_BASE_BRANCH", "main")


def configured():
    return bool(os.environ.get("DEVIN_API_KEY"))


def prompt_for(req, requester, approver_note):
    return (
        f"Change request #{req['id']} for the AcmePay internal tools app in github.com/{REPO} "
        f"(Flask + SQLite in app/). Branch from `{BASE_BRANCH}` and open a PR back into it. Don't merge.\n\n"
        f"Requested by {requester['name']} ({requester['role']}, {requester['team']}):\n"
        f"Title: {req['title']}\n{req['details']}\n\n"
        + (f"Admin note: {approver_note}\n\n" if approver_note else "")
        + "Keep the change small and follow the existing code style. Check permissions on the server "
        "(see app/access.py), write user-visible changes to the audit log like existing actions do, "
        "and say in the PR description how you tested it and what the requester should look at."
    )


def _v3():
    return os.environ.get("DEVIN_API_KEY", "").startswith("cog_")


def _call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def start(prompt, title):
    body = {"prompt": prompt, "title": title, "tags": ["acmepay-change-request"]}
    if _v3():
        if not ORG_ID:
            raise RuntimeError("DEVIN_ORG_ID must be set to use a service user key")
        r = _call("POST", f"/v3/organizations/{ORG_ID}/sessions", body)
    else:
        r = _call("POST", "/v1/sessions", dict(body, idempotent=True))
    return {"session_id": r["session_id"], "url": r["url"]}


def status(session_id):
    r = _call("GET", f"/v3/organizations/{ORG_ID}/sessions/{session_id}" if _v3() else f"/v1/session/{session_id}")
    pr = (r.get("pull_request") or {}).get("url") or next(
        (p.get("pr_url") or p.get("url") for p in r.get("pull_requests") or [] if p.get("pr_url") or p.get("url")), None)
    return {"status": r.get("status_enum") or r.get("status"), "pr_url": pr}
