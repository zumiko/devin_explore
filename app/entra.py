"""Sign in with Microsoft (Entra ID).

Turned on by setting ENTRA_TENANT_ID, ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET. Staff sign in on Microsoft's page
(password, MFA and device rules are Entra's job); the app never sees a password. Their role comes from Entra
group membership, so access is granted and revoked in Entra, not here. Without those settings the demo keeps the
"Acting as" switcher.
"""
import json
import os
import urllib.request

import msal

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "GroupMember.Read.All"]
# Entra security group display name -> app role. Highest privilege wins if someone is in several.
GROUP_ROLES = {
    "AcmePay-Admin": "admin",
    "AcmePay-Compliance-Lead": "compliance_lead",
    "AcmePay-KYC-Senior": "kyc_senior",
    "AcmePay-KYC-Analyst": "kyc_analyst",
    "AcmePay-Support-Lead": "support_lead",
    "AcmePay-Finance": "finance",
    "AcmePay-Engineer": "engineer",
    "AcmePay-PM": "pm",
    "AcmePay-Support-Agent": "support_agent",
}


def enabled():
    return all(os.environ.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"))


def _app():
    return msal.ConfidentialClientApplication(
        os.environ["ENTRA_CLIENT_ID"], client_credential=os.environ["ENTRA_CLIENT_SECRET"],
        authority=f"https://login.microsoftonline.com/{os.environ['ENTRA_TENANT_ID']}")


def start_login(redirect_uri):
    """Returns (flow, url). Store the flow in the session; it carries the state and PKCE values."""
    flow = _app().initiate_auth_code_flow(SCOPES, redirect_uri=redirect_uri)
    return flow, flow["auth_uri"]


def finish_login(flow, args):
    """Validates the callback (state, PKCE, ID token signature/audience/nonce via MSAL). Returns identity + role."""
    result = _app().acquire_token_by_auth_code_flow(flow, args)
    if "error" in result:
        raise PermissionError(result.get("error_description") or result["error"])
    claims = result["id_token_claims"]
    if claims.get("tid") != os.environ["ENTRA_TENANT_ID"]:
        raise PermissionError("Account is not from this organisation's directory.")
    req = urllib.request.Request(f"{GRAPH}/me/transitiveMemberOf/microsoft.graph.group?$select=displayName&$top=999",
                                 headers={"Authorization": "Bearer " + result["access_token"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        groups = {g["displayName"] for g in json.load(r)["value"]}
    role = next((role for name, role in GROUP_ROLES.items() if name in groups), None)
    return {"oid": claims["oid"], "name": claims.get("name"), "email": claims.get("preferred_username", ""),
            "role": role, "groups": sorted(groups & GROUP_ROLES.keys())}


def logout_url(post_logout_redirect):
    return (f"https://login.microsoftonline.com/{os.environ['ENTRA_TENANT_ID']}/oauth2/v2.0/logout"
            f"?post_logout_redirect_uri={urllib.request.quote(post_logout_redirect, safe='')}")
