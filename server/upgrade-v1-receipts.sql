-- Apply exactly once only to a database created from the pre-receipt schema.
-- Fresh databases should execute schema.sql instead and must not run this file.
PRAGMA foreign_keys = ON;

ALTER TABLE observations ADD COLUMN verification_state TEXT NOT NULL
  DEFAULT 'client_attested'
  CHECK (verification_state IN ('server_verified_shared','client_attested'));

CREATE TABLE shared_detection_receipts (
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
CREATE INDEX shared_detection_receipts_expiry
  ON shared_detection_receipts(expires_at);
CREATE INDEX shared_detection_receipts_observation
  ON shared_detection_receipts(install_id, client_observation_id);

CREATE TABLE external_rate_gates (
  name            TEXT PRIMARY KEY,
  next_allowed_at INTEGER NOT NULL
);
