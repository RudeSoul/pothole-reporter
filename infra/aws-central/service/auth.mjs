import {
  createHash,
  createPublicKey,
  randomUUID,
  verify as verifySignature,
} from "node:crypto";

const RAW_P256_SPKI_PREFIX = Buffer.from(
  "3059301306072a8648ce3d020106082a8648ce3d030107034200",
  "hex",
);

export const sha256Hex = (value) => createHash("sha256").update(value).digest("hex");

export function decodeBase64(value, code = "bad_base64") {
  if (typeof value !== "string" || !value.trim()) {
    const error = new Error("A required base64 value is missing.");
    error.code = code;
    throw error;
  }
  const normalized = value.trim().replace(/-/g, "+").replace(/_/g, "/");
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(normalized)) {
    const error = new Error("A base64 value is invalid.");
    error.code = code;
    throw error;
  }
  const bytes = Buffer.from(normalized, "base64");
  if (!bytes.length) {
    const error = new Error("A base64 value is empty.");
    error.code = code;
    throw error;
  }
  return bytes;
}

export function installationPublicKey(encoded) {
  const bytes = decodeBase64(encoded, "bad_public_key");
  let der;
  let format;
  if (bytes.length === 65 && bytes[0] === 0x04) {
    der = Buffer.concat([RAW_P256_SPKI_PREFIX, bytes]);
    format = "raw";
  } else if (bytes.length >= 80 && bytes.length <= 160 && bytes[0] === 0x30) {
    der = bytes;
    format = "spki";
  } else {
    const error = new Error("public_key must be a raw or SPKI P-256 public key.");
    error.code = "bad_public_key";
    throw error;
  }
  try {
    const key = createPublicKey({ key: der, format: "der", type: "spki" });
    if (key.asymmetricKeyType !== "ec"
        || key.asymmetricKeyDetails?.namedCurve !== "prime256v1") {
      throw new Error("not P-256");
    }
    return {
      encoded: encoded.trim(),
      bytes,
      format,
      key,
      installId: sha256Hex(bytes).slice(0, 32),
    };
  } catch {
    const error = new Error("The P-256 public key could not be imported.");
    error.code = "bad_public_key";
    throw error;
  }
}

export function canonicalRequest({ method, path, timestamp, idempotencyKey, body }) {
  return [
    String(method || "").toUpperCase(),
    path,
    timestamp,
    idempotencyKey || "",
    sha256Hex(body),
  ].join("\n");
}

export function verifyInstallationSignature({
  installation,
  signature,
  method,
  path,
  timestamp,
  idempotencyKey,
  body,
}) {
  const publicKey = installationPublicKey(installation.public_key).key;
  const signed = Buffer.from(canonicalRequest({
    method,
    path,
    timestamp,
    idempotencyKey,
    body,
  }));
  const bytes = decodeBase64(signature, "bad_signature");
  try {
    if (bytes.length === 64) {
      return verifySignature("sha256", signed, {
        key: publicKey,
        dsaEncoding: "ieee-p1363",
      }, bytes);
    }
    return verifySignature("sha256", signed, publicKey, bytes);
  } catch {
    return false;
  }
}

export function newRequestId() {
  return randomUUID();
}
