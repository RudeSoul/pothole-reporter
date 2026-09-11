PRAGMA foreign_keys = ON;

-- A pseudonymous installation is a public signing key, not an account. No name,
-- phone number or email address is collected. Public APIs never return install_id.
CREATE TABLE IF NOT EXISTS installations (
  id                TEXT PRIMARY KEY,
  public_key        TEXT NOT NULL,
  public_key_format TEXT NOT NULL DEFAULT 'raw',
  created_at        INTEGER NOT NULL,
  last_seen_at      INTEGER NOT NULL,
  integrity_state   TEXT,
  revoked_at        INTEGER
);

-- A canonical physical road defect. Reports remain factual observations; this
-- service does not track or infer whether a defect was subsequently repaired.
CREATE TABLE IF NOT EXISTS potholes (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  lat                  REAL NOT NULL,
  lng                  REAL NOT NULL,
  geohash              TEXT,
  body_lgd             TEXT,
  town                 TEXT,
  damage_type          TEXT NOT NULL
                           CHECK (damage_type IN ('pothole_cavity','failed_patch',
                             'surface_breakup','rut_or_depression','other_road_damage')),
  size                 TEXT CHECK (size IS NULL OR size IN ('small','medium','large')),
  first_seen_at        INTEGER NOT NULL,
  last_seen_at         INTEGER NOT NULL,
  seen_count           INTEGER NOT NULL DEFAULT 1,
  created_request_id   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS potholes_box ON potholes(lat, lng);
CREATE INDEX IF NOT EXISTS potholes_body ON potholes(body_lgd, last_seen_at);
CREATE INDEX IF NOT EXISTS potholes_geohash ON potholes(geohash);

-- Every accepted sighting is retained as a small factual record, even when it joins
-- an existing pothole. Images are never retained; only their SHA-256 is recorded.
CREATE TABLE IF NOT EXISTS observations (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  pothole_id            INTEGER NOT NULL REFERENCES potholes(id),
  install_id            TEXT NOT NULL REFERENCES installations(id),
  request_id            TEXT NOT NULL UNIQUE,
  client_observation_id TEXT NOT NULL,
  observed_at           INTEGER NOT NULL,
  lat                   REAL NOT NULL,
  lng                   REAL NOT NULL,
  gps_accuracy_m        REAL,
  heading_deg           REAL,
  speed_mps             REAL,
  capture_source        TEXT NOT NULL DEFAULT 'manual'
                            CHECK (capture_source IN
                              ('manual','drive_live','drive_vod','imported_video')),
  location_source       TEXT NOT NULL DEFAULT 'device_gps'
                            CHECK (location_source IN
                              ('device_gps','gpx_timestamp','current_position_confirmed','none')),
  damage_type           TEXT NOT NULL
                            CHECK (damage_type IN ('pothole_cavity','failed_patch',
                              'surface_breakup','rut_or_depression','other_road_damage')),
  size                  TEXT CHECK (size IS NULL OR size IN ('small','medium','large')),
  image_hash            TEXT NOT NULL,
  detector_provider     TEXT CHECK (detector_provider IN
                              ('shared_server','personal_openai','own_key')),
  verification_state    TEXT NOT NULL DEFAULT 'client_attested'
                            CHECK (verification_state IN
                              ('server_verified_shared','client_attested')),
  detector_model        TEXT,
  prompt_version        TEXT,
  schema_version        INTEGER,
  duplicate_distance_m  REAL,
  UNIQUE(install_id, client_observation_id)
);
CREATE INDEX IF NOT EXISTS observations_pothole ON observations(pothole_id, observed_at);
CREATE INDEX IF NOT EXISTS observations_install ON observations(install_id, observed_at);

-- One installation counts once toward a pothole's impact, regardless of retries or
-- repeated drives. The exact observations remain available for audit.
CREATE TABLE IF NOT EXISTS pothole_observers (
  pothole_id    INTEGER NOT NULL REFERENCES potholes(id),
  install_id    TEXT NOT NULL REFERENCES installations(id),
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  PRIMARY KEY (pothole_id, install_id)
);

-- Contract data is refreshed independently of app releases. body_lgd is the
-- authoritative awarding-body join key used by Karnataka routing.
CREATE TABLE IF NOT EXISTS tenders (
  tender_number TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  location      TEXT,
  contractor    TEXT,
  published     TEXT,
  body_lgd      TEXT,
  source_name   TEXT,
  source_url    TEXT,
  updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS tenders_body ON tenders(body_lgd);

-- Long-lived aggregate request counts. Per-request IDs stay in structured Worker
-- logs; the database keeps only counts needed to show impact.
CREATE TABLE IF NOT EXISTS request_metrics_daily (
  day           TEXT NOT NULL,
  route         TEXT NOT NULL,
  outcome       TEXT NOT NULL,
  vision_mode   TEXT NOT NULL DEFAULT 'none',
  request_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, route, outcome, vision_mode)
);

-- Exactly-once aggregate detector activity by capture/location provenance. This table
-- deliberately has no installation or request identifier; its increment commits in the
-- same idempotency batch as a fresh shared detection or personal-key activity heartbeat.
CREATE TABLE IF NOT EXISTS capture_metrics_daily (
  day             TEXT NOT NULL,
  capture_source  TEXT NOT NULL
                       CHECK (capture_source IN
                         ('manual','drive_live','drive_vod','imported_video')),
  location_source TEXT NOT NULL
                       CHECK (location_source IN
                         ('device_gps','gpx_timestamp','current_position_confirmed','none')),
  vision_mode     TEXT NOT NULL CHECK (vision_mode IN ('shared_detect','own_key')),
  outcome         TEXT NOT NULL,
  request_count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, capture_source, location_source, vision_mode, outcome)
);

-- Enables daily/monthly active-install counts without storing raw analytics events.
CREATE TABLE IF NOT EXISTS installation_activity_daily (
  day           TEXT NOT NULL,
  install_id    TEXT NOT NULL REFERENCES installations(id),
  request_count INTEGER NOT NULL DEFAULT 0,
  last_seen_at  INTEGER NOT NULL,
  PRIMARY KEY(day, install_id)
);

-- Successful mutating/costly responses are cached by caller-supplied idempotency key.
-- Request hashes prevent a key being reused for a different operation.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  install_id      TEXT NOT NULL REFERENCES installations(id),
  route           TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash    TEXT NOT NULL,
  status_code     INTEGER NOT NULL,
  response_json   TEXT NOT NULL,
  created_at      INTEGER NOT NULL,
  PRIMARY KEY(install_id, route, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idempotency_keys_retention
  ON idempotency_keys(route, created_at);

-- A short lease closes the read-before-act race for paid or mutating operations.
-- Completed responses move to idempotency_keys; abandoned claims may be taken over
-- after the lease expires. No request body or image is retained here.
CREATE TABLE IF NOT EXISTS idempotency_claims (
  install_id      TEXT NOT NULL REFERENCES installations(id),
  route           TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash    TEXT NOT NULL,
  claim_token     TEXT NOT NULL,
  claimed_at      INTEGER NOT NULL,
  PRIMARY KEY(install_id, route, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idempotency_claims_age
  ON idempotency_claims(claimed_at);

-- Correctness-critical shared-credit counters use an atomic D1 upsert rather than
-- eventually-consistent KV read/modify/write.
CREATE TABLE IF NOT EXISTS usage_counters (
  scope       TEXT NOT NULL,
  counter_key TEXT NOT NULL,
  used        INTEGER NOT NULL DEFAULT 0,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY(scope, counter_key)
);

-- A successful shared-server verdict authorizes exactly one factual observation.
-- The image itself is never retained: the receipt binds only its SHA-256 digest,
-- the canonical verdict, the signed installation, and (when supplied) coordinates.
CREATE TABLE IF NOT EXISTS shared_detection_receipts (
  receipt_id                    TEXT PRIMARY KEY,
  install_id                   TEXT NOT NULL REFERENCES installations(id),
  client_observation_id        TEXT NOT NULL,
  image_hash                   TEXT NOT NULL,
  detection_lat                REAL,
  detection_lng                REAL,
  damage_type                  TEXT NOT NULL
                                   CHECK (damage_type IN ('pothole_cavity','failed_patch',
                                     'surface_breakup','rut_or_depression','other_road_damage')),
  size                         TEXT CHECK (size IS NULL OR size IN ('small','medium','large')),
  backend_provider             TEXT NOT NULL,
  detector_model               TEXT,
  prompt_version               TEXT NOT NULL,
  schema_version               INTEGER NOT NULL,
  issued_at                    INTEGER NOT NULL,
  expires_at                   INTEGER NOT NULL,
  consumed_at                  INTEGER,
  consumed_request_id          TEXT,
  consumed_client_observation_id TEXT
);
CREATE INDEX IF NOT EXISTS shared_detection_receipts_expiry
  ON shared_detection_receipts(expires_at);
CREATE INDEX IF NOT EXISTS shared_detection_receipts_observation
  ON shared_detection_receipts(install_id, client_observation_id);

-- Public Nominatim permits at most one request per second for an entire application.
-- This shared D1 lease is a correctness gate across Worker isolates, not a cache.
CREATE TABLE IF NOT EXISTS external_rate_gates (
  name            TEXT PRIMARY KEY,
  next_allowed_at INTEGER NOT NULL
);
