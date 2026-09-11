import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));

test("v2 provenance upgrade preserves legacy rows with explicit conservative defaults", () => {
  const database = new DatabaseSync(":memory:");
  try {
    database.exec(`
      PRAGMA foreign_keys = ON;
      CREATE TABLE installations (id TEXT PRIMARY KEY);
      CREATE TABLE potholes (id INTEGER PRIMARY KEY);
      CREATE TABLE observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pothole_id INTEGER NOT NULL REFERENCES potholes(id),
        install_id TEXT NOT NULL REFERENCES installations(id),
        request_id TEXT NOT NULL UNIQUE,
        client_observation_id TEXT NOT NULL,
        observed_at INTEGER NOT NULL,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        UNIQUE(install_id, client_observation_id)
      );
      INSERT INTO installations(id) VALUES ('legacy-install');
      INSERT INTO potholes(id) VALUES (1);
      INSERT INTO observations
        (pothole_id,install_id,request_id,client_observation_id,observed_at,lat,lng)
      VALUES (1,'legacy-install','legacy-request','legacy-observation',1,12.9,77.6);
    `);

    database.exec(readFileSync(
      resolve(here, "../upgrade-v2-capture-provenance.sql"), "utf8"));

    const legacy = database.prepare(
      `SELECT capture_source,location_source FROM observations WHERE id=1`).get();
    assert.equal(legacy.capture_source, "manual");
    assert.equal(legacy.location_source, "device_gps");
    database.prepare(
      `INSERT INTO capture_metrics_daily
         (day,capture_source,location_source,vision_mode,outcome,request_count)
       VALUES ('2026-09-11','imported_video','none','shared_detect','undamaged',1)`
    ).run();
    assert.throws(() => database.prepare(
      `INSERT INTO capture_metrics_daily
         (day,capture_source,location_source,vision_mode,outcome,request_count)
       VALUES ('2026-09-11','forged','none','shared_detect','undamaged',1)`
    ).run(), /CHECK constraint failed/);
  } finally {
    database.close();
  }
});
