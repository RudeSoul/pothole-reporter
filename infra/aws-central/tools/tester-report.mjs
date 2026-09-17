#!/usr/bin/env node
// Per-installation tester report: what each tester did, what failed for them, and what
// they said. Reads the metrics and records tables; writes nothing.
//
//   AWS_PROFILE=pothole AWS_REGION=ap-south-1 node infra/aws-central/tools/tester-report.mjs [days=7] [out-dir=.]
//
// Prints a summary and writes tester-usage.csv and tester-feedback.csv to out-dir.

import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, QueryCommand, ScanCommand } from "@aws-sdk/lib-dynamodb";

const prefix = process.env.PROJECT_PREFIX || "pothole-reporter-central";
const days = Math.max(1, Number(process.argv[2] || 7));
const outDir = process.argv[3] || ".";
const client = DynamoDBDocumentClient.from(new DynamoDBClient({}));

async function all(command, input) {
  const items = [];
  let ExclusiveStartKey;
  do {
    const page = await client.send(new command({ ...input, ExclusiveStartKey }));
    items.push(...(page.Items || []));
    ExclusiveStartKey = page.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return items;
}

const dayList = Array.from({ length: days }, (_, index) =>
  new Date(Date.now() - index * 86_400_000).toISOString().slice(0, 10)).reverse();

const metrics = (await Promise.all(dayList.map((day) => all(QueryCommand, {
  TableName: `${prefix}-metrics`,
  KeyConditionExpression: "#day = :day",
  ExpressionAttributeNames: { "#day": "day" },
  ExpressionAttributeValues: { ":day": day },
})))).flat();

const feedback = (await all(ScanCommand, {
  TableName: `${prefix}-records`,
  FilterExpression: "begins_with(pk, :feedback)",
  ExpressionAttributeValues: { ":feedback": "FEEDBACK#" },
})).filter((item) => item.created_at >= Date.parse(`${dayList[0]}T00:00:00Z`))
  .sort((left, right) => left.created_at - right.created_at);

const installs = new Map();
const install = (id) => {
  if (!installs.has(id)) {
    installs.set(id, {
      install_id: id, active_days: new Set(), last_seen_at: 0, requests: 0,
      detections: 0, damaged: 0, undamaged: 0, reports: 0, errors: 0, limit_hits: 0,
      error_codes: new Map(), feedback: 0, ratings: [], email: "",
    });
  }
  return installs.get(id);
};

for (const item of metrics) {
  if (item.metric.startsWith("active#")) {
    const row = install(item.metric.slice(7));
    row.active_days.add(item.day);
    row.last_seen_at = Math.max(row.last_seen_at, Number(item.last_seen_at || 0));
  } else if (item.metric.startsWith("install#")) {
    const [, id, route, outcome, status] = item.metric.split("#");
    const count = Number(item.request_count || 0);
    const row = install(id);
    row.requests += count;
    if (route === "/v1/vision/detect" && ["damaged", "undamaged"].includes(outcome)) {
      row.detections += count;
      row[outcome] += count;
    }
    if (route === "/v1/potholes/report" && ["created", "deduplicated"].includes(outcome)) {
      row.reports += count;
    }
    if (status === "error") {
      row.errors += count;
      if (/limit|budget/.test(outcome)) row.limit_hits += count;
      row.error_codes.set(outcome, (row.error_codes.get(outcome) || 0) + count);
    }
  }
}
for (const item of feedback) {
  const row = install(item.install_id);
  row.feedback += 1;
  if (item.rating) row.ratings.push(item.rating);
  if (item.email) row.email = item.email;
}

const csv = (rows) => rows.map((row) => row.map((value) => {
  const text = value == null ? "" : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}).join(",")).join("\n") + "\n";

const usage = [...installs.values()].sort((left, right) => right.last_seen_at - left.last_seen_at);
writeFileSync(join(outDir, "tester-usage.csv"), csv([
  ["install_id", "email_from_feedback", "active_days", "last_seen_utc", "detections", "damaged",
    "undamaged", "reports", "errors", "limit_hits", "top_errors", "feedback_count", "avg_rating"],
  ...usage.map((row) => [
    row.install_id, row.email, row.active_days.size,
    row.last_seen_at ? new Date(row.last_seen_at).toISOString() : "",
    row.detections, row.damaged, row.undamaged, row.reports, row.errors, row.limit_hits,
    [...row.error_codes].sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([code, count]) => `${code}:${count}`).join(" "),
    row.feedback,
    row.ratings.length ? (row.ratings.reduce((a, b) => a + b, 0) / row.ratings.length).toFixed(1) : "",
  ]),
]));
writeFileSync(join(outDir, "tester-feedback.csv"), csv([
  ["created_utc", "install_id", "email", "rating", "test_mode", "app_version", "device", "text"],
  ...feedback.map((item) => [
    new Date(item.created_at).toISOString(), item.install_id, item.email, item.rating,
    item.test_mode, item.app_version, item.device, item.text,
  ]),
]));

const active = usage.filter((row) => row.active_days.size);
const errorTotals = new Map();
for (const row of usage) {
  for (const [code, count] of row.error_codes) errorTotals.set(code, (errorTotals.get(code) || 0) + count);
}
const ratings = feedback.map((item) => item.rating).filter(Boolean);
console.log(`Tester report, ${dayList[0]} to ${dayList.at(-1)} (UTC)`);
console.log(`Active installations: ${active.length}`);
console.log(`Installations that filed a report: ${usage.filter((row) => row.reports).length}`);
console.log(`Detections: ${usage.reduce((sum, row) => sum + row.detections, 0)}, reports: ${usage.reduce((sum, row) => sum + row.reports, 0)}`);
console.log(`Feedback entries: ${feedback.length}${ratings.length ? `, average rating ${(ratings.reduce((a, b) => a + b, 0) / ratings.length).toFixed(1)} from ${ratings.length}` : ""}`);
console.log("Top errors:");
for (const [code, count] of [...errorTotals].sort((a, b) => b[1] - a[1]).slice(0, 8)) {
  console.log(`  ${code}: ${count}`);
}
console.log(`Wrote ${join(outDir, "tester-usage.csv")} and ${join(outDir, "tester-feedback.csv")}`);
