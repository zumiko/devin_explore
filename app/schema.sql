CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  role TEXT NOT NULL,
  team TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  dob TEXT NOT NULL,
  address TEXT NOT NULL,
  state TEXT NOT NULL,
  ssn_last4 TEXT NOT NULL,
  account_type TEXT NOT NULL,
  status TEXT NOT NULL,
  signup_at TEXT NOT NULL,
  phone TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  dob TEXT NOT NULL,
  country TEXT NOT NULL,
  list_name TEXT NOT NULL,
  reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kyc_cases (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status TEXT NOT NULL,
  assigned_to INTEGER REFERENCES users(id),
  risk_score INTEGER NOT NULL,
  flags_json TEXT NOT NULL,
  vendor_ref TEXT NOT NULL,
  requires_second_review INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  sla_due_at TEXT NOT NULL,
  first_decision_by INTEGER REFERENCES users(id),
  decision TEXT,
  decision_reason TEXT,
  decision_note TEXT,
  decided_by INTEGER REFERENCES users(id),
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  type TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  description TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY,
  transaction_id INTEGER NOT NULL REFERENCES transactions(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL,
  note TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  step INTEGER NOT NULL,
  required_role TEXT NOT NULL,
  status TEXT NOT NULL,
  decided_by INTEGER REFERENCES users(id),
  decided_at TEXT,
  comment TEXT
);

CREATE TABLE IF NOT EXISTS feature_flags (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL,
  owner_id INTEGER NOT NULL REFERENCES users(id),
  enabled INTEGER NOT NULL,
  rollout_pct INTEGER NOT NULL,
  target_customers_json TEXT NOT NULL,
  critical INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id INTEGER NOT NULL,
  before_json TEXT,
  after_json TEXT,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_checks (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES kyc_cases(id),
  run_by INTEGER REFERENCES users(id),
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  score REAL NOT NULL,
  verdict TEXT NOT NULL,
  id_image BLOB NOT NULL,
  id_image_type TEXT NOT NULL,
  selfie_image BLOB NOT NULL,
  selfie_image_type TEXT NOT NULL,
  id_face BLOB NOT NULL,
  selfie_face BLOB NOT NULL,
  details_json TEXT NOT NULL,
  ocr_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  case_id INTEGER REFERENCES kyc_cases(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  channel TEXT NOT NULL,
  to_addr TEXT NOT NULL,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  provider TEXT,
  provider_id TEXT,
  error TEXT,
  ics TEXT,
  sent_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_links (
  token TEXT PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES kyc_cases(id),
  purpose TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
