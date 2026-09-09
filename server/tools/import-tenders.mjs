#!/usr/bin/env node

import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";

const sourcePath = resolve(process.argv[2] || "../data/tenders-karnataka.json");
const sourceName = String(process.argv[3] || "Karnataka public tender export").trim();
const sourceUrl = String(process.argv[4] || "").trim();

function fail(message) {
  process.stderr.write(`Tender import failed: ${message}\n`);
  process.exitCode = 1;
}

function cleaned(value, maximum) {
  if (value == null) return "";
  return String(value)
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maximum);
}

function sqlText(value, maximum) {
  const text = cleaned(value, maximum);
  return text ? `'${text.replaceAll("'", "''")}'` : "NULL";
}

let records;
try {
  records = JSON.parse(readFileSync(sourcePath, "utf8"));
} catch (error) {
  fail(`${sourcePath}: ${error.message}`);
  process.exit();
}
if (!Array.isArray(records)) {
  fail(`${sourcePath} must contain a JSON array.`);
  process.exit();
}

const importedAt = Date.now();
let imported = 0;
let skippedUnscoped = 0;
let skippedInvalid = 0;

process.stdout.write("PRAGMA foreign_keys = ON;\nBEGIN TRANSACTION;\n");
for (const record of records) {
  const tenderNumber = cleaned(record && (record.tn || record.tender_number), 300);
  const title = cleaned(record && (record.t || record.title), 4000);
  const bodyLgd = cleaned(record && (record.b || record.body_lgd), 64);
  if (!tenderNumber || !title) {
    skippedInvalid++;
    continue;
  }
  // The resolver is deliberately body-scoped. Rows without the awarding body's LGD
  // key cannot be safely attached to a location, so they stay out of serving data.
  if (!bodyLgd) {
    skippedUnscoped++;
    continue;
  }
  process.stdout.write(
    "INSERT INTO tenders "
      + "(tender_number,title,location,contractor,published,body_lgd,source_name,source_url,updated_at) VALUES ("
      + `${sqlText(tenderNumber, 300)},${sqlText(title, 4000)},`
      + `${sqlText(record.loc || record.location, 500)},`
      + `${sqlText(record.c || record.contractor, 500)},`
      + `${sqlText(record.d || record.published, 64)},${sqlText(bodyLgd, 64)},`
      + `${sqlText(sourceName, 200)},${sqlText(sourceUrl, 1000)},${importedAt}) `
      + "ON CONFLICT(tender_number) DO UPDATE SET "
      + "title=excluded.title,location=excluded.location,contractor=excluded.contractor,"
      + "published=excluded.published,body_lgd=excluded.body_lgd,"
      + "source_name=excluded.source_name,source_url=excluded.source_url,"
      + "updated_at=excluded.updated_at;\n",
  );
  imported++;
}
process.stdout.write("COMMIT;\n");
process.stderr.write(
  `Prepared ${imported} scoped tenders from ${basename(sourcePath)}; `
    + `skipped ${skippedUnscoped} without body_lgd and ${skippedInvalid} invalid rows.\n`,
);
