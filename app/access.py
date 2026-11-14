"""Roles and the default permissions each one starts with. Admins can change permissions at /admin/access."""

ROLES = [
    ("kyc_analyst", "KYC analyst", "Works individual identity-check cases"),
    ("kyc_senior", "KYC senior", "Hard cases and the second sign-off"),
    ("compliance_lead", "Compliance lead", "Owns KYC; answers to regulators"),
    ("support_agent", "Support agent", "Customer issues, small refunds"),
    ("support_lead", "Support lead", "Runs the support team, approves larger refunds"),
    ("finance", "Finance", "Signs off big refunds, owns refund limits"),
    ("pm", "PM", "Rolls out features"),
    ("engineer", "Engineer", "Runs the platform"),
    ("admin", "Admin (IT)", "Runs the tool: people, access, audit. Doesn't move money or approve identities"),
]
ROLE_KEYS = [r[0] for r in ROLES]
KYC_ROLES = {"kyc_analyst", "kyc_senior", "compliance_lead", "admin"}

# perm: (area, plain-English description, roles that have it by default)
PERMISSIONS = {
    "kyc_view": ("KYC", "Open the KYC queue and cases", KYC_ROLES),
    "kyc_work": ("KYC", "Pick up cases, decide them, contact customers", {"kyc_analyst", "kyc_senior", "compliance_lead"}),
    "kyc_view_all": ("KYC", "See the whole queue, not just own and unassigned cases", {"kyc_senior", "compliance_lead", "admin"}),
    "kyc_second_review": ("KYC", "Give the second sign-off on high-risk and escalated cases", {"kyc_senior", "compliance_lead"}),
    "kyc_team_view": ("KYC", "Team overview: sign-offs waiting, overdue cases, who holds what", {"kyc_senior", "compliance_lead"}),
    "kyc_dashboard": ("KYC", "KYC dashboard: trends and reviewer performance", {"compliance_lead", "admin"}),
    "customer_view": ("Customers", "Look up customers", KYC_ROLES | {"support_agent", "support_lead", "finance"}),
    "customer_pii": ("Customers", "See personal details: date of birth, address, SSN last 4", KYC_ROLES),
    "refund_view": ("Refunds", "See refunds", {"support_agent", "support_lead", "finance", "admin"}),
    "refund_request": ("Refunds", "Issue refunds", {"support_agent", "support_lead"}),
    "refund_export": ("Refunds", "Export sent refunds (CSV) for reconciliation", {"support_lead", "finance"}),
    "approvals_view": ("Refunds", "Approve refunds above the limit", {"support_lead", "finance"}),
    "refund_dashboard": ("Refunds", "Refund dashboard", {"support_lead", "finance", "pm", "admin"}),
    "flag_view": ("Feature flags", "See feature flags", {"pm", "engineer", "admin"}),
    "flag_edit": ("Feature flags", "Change feature flags", {"pm", "engineer", "admin"}),
    "flag_edit_critical": ("Feature flags", "Change critical flags (payments, KYC)", {"engineer", "admin"}),
    "settings_view": ("Rules", "See business rules", {"compliance_lead", "finance", "admin"}),
    "kyc_rules_edit": ("Rules", "Change KYC rules (face match, watchlist, deadlines)", {"compliance_lead"}),
    "refund_rules_edit": ("Rules", "Change refund limits", {"finance"}),
    "audit_view": ("Audit", "Audit log: feature flag changes", {"compliance_lead", "support_lead", "finance", "engineer", "admin"}),
    "audit_view_all": ("Audit", "Audit log: everything (KYC, refunds, rules, access)", {"compliance_lead", "support_lead", "finance", "admin"}),
    "ask_data": ("Other", "Ask the data in plain English", set(ROLE_KEYS) - {"kyc_analyst", "support_agent"}),
    "change_request": ("Changes", "Ask for changes to these tools",
                       {"kyc_senior", "compliance_lead", "support_lead", "finance", "pm", "engineer", "admin"}),
    "change_approve": ("Changes", "Approve change requests (sends them to Devin)", {"admin"}),
    "change_ship": ("Changes", "Review Devin's code and mark changes live", {"engineer"}),
    "users_manage": ("Other", "Manage people and permissions (this page)", {"admin"}),
}
LOCKED = {("admin", "users_manage")}  # so admins can't lock themselves out


def default_rows():
    return [(role, perm) for perm, (_, _, roles) in PERMISSIONS.items() for role in ROLE_KEYS if role in roles]


def setting_perm(key):
    return "refund_rules_edit" if key.startswith("refund_") else "kyc_rules_edit"
