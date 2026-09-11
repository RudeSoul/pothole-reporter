-- Apply exactly once only to a database that already has the v1 receipt schema.
-- Fresh databases should execute schema.sql instead and must not run this file.
PRAGMA foreign_keys = ON;

-- Old observations predate explicit source labels. Preserve them with conservative,
-- deterministic defaults; an upgrade cannot reconstruct their original capture path.
ALTER TABLE observations ADD COLUMN capture_source TEXT NOT NULL
  DEFAULT 'manual'
  CHECK (capture_source IN ('manual','drive_live','drive_vod','imported_video'));

ALTER TABLE observations ADD COLUMN location_source TEXT NOT NULL
  DEFAULT 'device_gps'
  CHECK (location_source IN
    ('device_gps','gpx_timestamp','current_position_confirmed','none'));

-- Aggregate-only detector activity. No installation ID, request ID, coordinate, image,
-- filename or video identifier is stored here.
CREATE TABLE capture_metrics_daily (
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
