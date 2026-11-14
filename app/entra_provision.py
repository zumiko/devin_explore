"""Create the demo staff in an Entra test tenant: one security group per role, one account per seeded user.

Needs ENTRA_TENANT_ID / ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET for an app registration with Microsoft Graph
application permissions User.ReadWrite.All and Group.ReadWrite.All (admin consented). Safe to re-run: existing
groups/users are reused. Generated passwords are written to entra_test_users.local.json (git-ignored), never printed.

    python entra_provision.py
"""
import json
import os
import secrets
import urllib.error
import urllib.request

import msal

from entra import GRAPH, GROUP_ROLES
from seed import USERS

OUT = os.path.join(os.path.dirname(__file__), "entra_test_users.local.json")


def token():
    app = msal.ConfidentialClientApplication(
        os.environ["ENTRA_CLIENT_ID"], client_credential=os.environ["ENTRA_CLIENT_SECRET"],
        authority=f"https://login.microsoftonline.com/{os.environ['ENTRA_TENANT_ID']}")
    result = app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise SystemExit(result.get("error_description"))
    return result["access_token"]


def graph(tok, method, path, body=None):
    req = urllib.request.Request(GRAPH + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json",
                                          "ConsistencyLevel": "eventual"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r) if r.status != 204 else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        if e.code == 400 and "added object references already exist" in detail:
            return None
        raise SystemExit(f"{method} {path} failed: {e.code} {detail[:300]}")


def main():
    tok = token()
    org = graph(tok, "GET", "/organization")["value"][0]
    domain = next(d["name"] for d in org["verifiedDomains"] if d.get("isDefault"))
    role_group = {v: k for k, v in GROUP_ROLES.items()}
    group_ids = {}
    for name in GROUP_ROLES:
        found = graph(tok, "GET", f"/groups?$filter=displayName eq '{name}'")["value"]
        group_ids[name] = found[0]["id"] if found else graph(tok, "POST", "/groups", {
            "displayName": name, "mailEnabled": False, "mailNickname": name.lower(), "securityEnabled": True,
            "description": "AcmePay internal tools demo role"})["id"]
    saved = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for _id, name, role, team in USERS:
        upn = f"{name.lower().replace(' ', '.')}@{domain}"
        found = graph(tok, "GET", f"/users?$filter=userPrincipalName eq '{upn}'")["value"]
        if found:
            uid = found[0]["id"]
        else:
            password = secrets.token_urlsafe(12) + "!a1"
            uid = graph(tok, "POST", "/users", {
                "accountEnabled": True, "displayName": name, "mailNickname": upn.split("@")[0],
                "userPrincipalName": upn, "department": team, "jobTitle": role.replace("_", " "),
                "passwordProfile": {"forceChangePasswordNextSignIn": False, "password": password}})["id"]
            saved[upn] = {"name": name, "role": role, "password": password}
        group = role_group[role]
        graph(tok, "POST", f"/groups/{group_ids[group]}/members/$ref", {"@odata.id": f"{GRAPH}/directoryObjects/{uid}"})
        print(f"{upn:45} -> {group}")
    with open(os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(saved, f, indent=2)
    print(f"Passwords for new accounts saved to {OUT}")


if __name__ == "__main__":
    main()
