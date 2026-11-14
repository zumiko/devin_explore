"""Starts a Devin session for an approved change request and reads back its status and PR.

Off unless DEVIN_API_KEY is set; without it the app records the request it would have sent.
"""
import json
import os
import urllib.request

API = "https://api.devin.ai/v1"
REPO = os.environ.get("CHANGE_REPO", "zumiko/devin_explore")
BASE_BRANCH = os.environ.get("CHANGE_BASE_BRANCH", "devin/face-match-upload")


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


def _call(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def start(prompt, title):
    r = _call("POST", "/sessions", {"prompt": prompt, "title": title, "idempotent": True,
                                    "tags": ["acmepay-change-request"]})
    return {"session_id": r["session_id"], "url": r["url"]}


def status(session_id):
    r = _call("GET", f"/session/{session_id}")
    pr = (r.get("pull_request") or {}).get("url") or next(
        (p.get("url") for p in r.get("pull_requests") or [] if p.get("url")), None)
    return {"status": r.get("status_enum") or r.get("status"), "pr_url": pr}
