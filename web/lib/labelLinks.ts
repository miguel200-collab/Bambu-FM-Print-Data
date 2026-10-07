// labelLinks.ts — signed, expiring links that open the "Label my print" page
// for ONE print (used by the future email reminder).
//
// A token is  base64url(JSON claims) + "." + base64url(HMAC-SHA256(secret, payload)).
// The claims are signed, not encrypted: they carry the print's id and a display
// filename/printer name (which are already in the email), plus an expiry.
//
// The link only OPENS the page; the student still presses "Succeeded" / "Failed",
// so email security scanners that prefetch links cannot record a label by
// accident. SERVER-ONLY: uses node:crypto and reads secrets.

import { createHmac, timingSafeEqual } from "node:crypto";
import { isJobId } from "./labeling";

// Same as the station's labeling window (database.LABEL_WINDOW_DAYS).
export const LABEL_TOKEN_TTL_S = 14 * 24 * 60 * 60;

const MIN_SECRET_LENGTH = 16;
const MAX_TOKEN_LENGTH = 2048;

export interface LabelClaims {
  jobId: string;
  exp: number; // unix seconds
  filename?: string;
  printer?: string;
}

export type VerifyResult =
  | { ok: true; claims: LabelClaims }
  | { ok: false; reason: "malformed" | "bad-signature" | "expired" };

const nowSeconds = () => Math.floor(Date.now() / 1000);

function mac(secret: string, data: string): Buffer {
  return createHmac("sha256", secret).update(data).digest();
}

function assertSecret(secret: string): void {
  if (typeof secret !== "string" || secret.length < MIN_SECRET_LENGTH) {
    throw new Error(`LABEL_LINK_SECRET must be at least ${MIN_SECRET_LENGTH} characters`);
  }
}

export function signLabelToken(
  claims: { jobId: string; filename?: string; printer?: string },
  secret: string,
  now: number = nowSeconds(),
  ttlSeconds: number = LABEL_TOKEN_TTL_S,
): string {
  assertSecret(secret);
  if (!isJobId(claims.jobId)) throw new Error("jobId must be a print job uuid");
  const payload = Buffer.from(
    JSON.stringify({
      j: claims.jobId,
      exp: now + ttlSeconds,
      f: claims.filename?.slice(0, 160),
      p: claims.printer?.slice(0, 80),
    }),
  ).toString("base64url");
  return `${payload}.${mac(secret, payload).toString("base64url")}`;
}

export function verifyLabelToken(
  token: unknown,
  secret: string,
  now: number = nowSeconds(),
): VerifyResult {
  assertSecret(secret);
  if (typeof token !== "string" || token.length > MAX_TOKEN_LENGTH) {
    return { ok: false, reason: "malformed" };
  }
  const parts = token.split(".");
  if (parts.length !== 2 || !parts[0] || !parts[1]) return { ok: false, reason: "malformed" };
  const [payload, signature] = parts;

  // Verify the signature BEFORE looking at the payload.
  const expected = mac(secret, payload);
  const given = Buffer.from(signature, "base64url");
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return { ok: false, reason: "bad-signature" };
  }

  let raw: { j?: unknown; exp?: unknown; f?: unknown; p?: unknown };
  try {
    raw = JSON.parse(Buffer.from(payload, "base64url").toString("utf-8"));
  } catch {
    return { ok: false, reason: "malformed" };
  }
  if (!isJobId(raw.j) || typeof raw.exp !== "number" || !Number.isFinite(raw.exp)) {
    return { ok: false, reason: "malformed" };
  }
  if (now >= raw.exp) return { ok: false, reason: "expired" };

  return {
    ok: true,
    claims: {
      jobId: raw.j,
      exp: raw.exp,
      filename: typeof raw.f === "string" ? raw.f : undefined,
      printer: typeof raw.p === "string" ? raw.p : undefined,
    },
  };
}

export function buildLabelUrl(baseUrl: string, token: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/label?t=${encodeURIComponent(token)}`;
}

// Constant-time string comparison for shared secrets.
export function safeEqual(a: string, b: string): boolean {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

// ---- feature flag + secrets (read from the environment) -------------------

// process.env is assignable to this; tests can pass a plain object.
export type Env = Record<string, string | undefined>;

export function emailLabelingEnabled(env: Env = process.env): boolean {
  return env.EMAIL_LABELING_ENABLED === "true";
}

export function labelLinkSecret(env: Env = process.env): string | null {
  const secret = env.LABEL_LINK_SECRET;
  return secret && secret.length >= MIN_SECRET_LENGTH ? secret : null;
}

// What the label page should do with a `?t=` token. Email links are a FUTURE,
// disabled feature: anything other than "flag on + secret set + valid token"
// returns no link, so the page falls back to the normal NetID form and behaves
// exactly as it does today.
export interface ResolvedLink {
  linked: { token: string; filename?: string; printer?: string } | null;
  problem: string | null;
}

export function resolveLabelLink(
  token: string | undefined,
  env: Env = process.env,
  now: number = nowSeconds(),
): ResolvedLink {
  if (!token || !emailLabelingEnabled(env)) return { linked: null, problem: null };
  const secret = labelLinkSecret(env);
  if (!secret) return { linked: null, problem: null };
  const verified = verifyLabelToken(token, secret, now);
  if (verified.ok) {
    return {
      linked: { token, filename: verified.claims.filename, printer: verified.claims.printer },
      problem: null,
    };
  }
  return {
    linked: null,
    problem:
      verified.reason === "expired"
        ? "That link has expired. You can still label your print below."
        : "That link is not valid. You can still label your print below.",
  };
}
