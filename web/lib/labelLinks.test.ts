import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  type Env,
  buildLabelUrl,
  emailLabelingEnabled,
  labelLinkSecret,
  LABEL_TOKEN_TTL_S,
  resolveLabelLink,
  safeEqual,
  signLabelToken,
  verifyLabelToken,
} from "./labelLinks";

const SECRET = "0123456789abcdef-secret";
const JOB = "123e4567-e89b-42d3-a456-426614174000";
const NOW = 1_800_000_000;

describe("sign / verify round trip", () => {
  it("verifies a fresh token and returns its claims", () => {
    const token = signLabelToken({ jobId: JOB, filename: "ab123_bracket.gcode.3mf", printer: "The Allee" }, SECRET, NOW);
    const r = verifyLabelToken(token, SECRET, NOW + 60);
    expect(r).toEqual({
      ok: true,
      claims: { jobId: JOB, exp: NOW + LABEL_TOKEN_TTL_S, filename: "ab123_bracket.gcode.3mf", printer: "The Allee" },
    });
  });

  it("works without the optional display fields", () => {
    const r = verifyLabelToken(signLabelToken({ jobId: JOB }, SECRET, NOW), SECRET, NOW);
    expect(r.ok && r.claims.filename).toBeUndefined();
  });

  it("is URL-safe (no +, / or = ) so it survives email clients", () => {
    const token = signLabelToken({ jobId: JOB, filename: "?>>>~~~ ñ.gcode.3mf" }, SECRET, NOW);
    expect(token).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  });

  it("expires after the ttl (14 days by default)", () => {
    const token = signLabelToken({ jobId: JOB }, SECRET, NOW);
    expect(verifyLabelToken(token, SECRET, NOW + LABEL_TOKEN_TTL_S - 1).ok).toBe(true);
    expect(verifyLabelToken(token, SECRET, NOW + LABEL_TOKEN_TTL_S)).toEqual({ ok: false, reason: "expired" });
    expect(LABEL_TOKEN_TTL_S).toBe(14 * 86400);
  });
});

describe("tampering", () => {
  const token = signLabelToken({ jobId: JOB, filename: "a.gcode.3mf" }, SECRET, NOW);
  const [payload, sig] = token.split(".");

  it("rejects a different secret", () => {
    expect(verifyLabelToken(token, "another-secret-value-xyz", NOW)).toEqual({ ok: false, reason: "bad-signature" });
  });

  it("rejects a modified payload (e.g. pointing the link at another print)", () => {
    const evil = Buffer.from(JSON.stringify({ j: "ffffffff-e89b-42d3-a456-426614174000", exp: NOW + 99999 })).toString("base64url");
    expect(verifyLabelToken(`${evil}.${sig}`, SECRET, NOW)).toEqual({ ok: false, reason: "bad-signature" });
  });

  it("rejects a modified or truncated signature", () => {
    const flipped = sig.slice(0, -1) + (sig.endsWith("A") ? "B" : "A");
    expect(verifyLabelToken(`${payload}.${flipped}`, SECRET, NOW).ok).toBe(false);
    expect(verifyLabelToken(`${payload}.${sig.slice(0, 10)}`, SECRET, NOW).ok).toBe(false);
  });

  it("rejects an extended expiry even if the attacker keeps the old signature", () => {
    const claims = JSON.parse(Buffer.from(payload, "base64url").toString());
    claims.exp += 10_000_000;
    const forged = Buffer.from(JSON.stringify(claims)).toString("base64url");
    expect(verifyLabelToken(`${forged}.${sig}`, SECRET, NOW).ok).toBe(false);
  });

  it("rejects malformed input of every shape", () => {
    for (const bad of ["", "abc", "a.b.c", ".", "a.", ".b", 123, null, undefined, {}, "x".repeat(5000)]) {
      const r = verifyLabelToken(bad, SECRET, NOW);
      expect(r.ok, String(bad)).toBe(false);
    }
  });

  it("a validly signed payload that is not JSON / has no job id is malformed, not accepted", () => {
    const sign = (p: string) => `${p}.${createHmac("sha256", SECRET).update(p).digest().toString("base64url")}`;
    const notJson = Buffer.from("not json").toString("base64url");
    const noJob = Buffer.from(JSON.stringify({ exp: NOW + 5 })).toString("base64url");
    const badJob = Buffer.from(JSON.stringify({ j: "../etc/passwd", exp: NOW + 5 })).toString("base64url");
    for (const p of [notJson, noJob, badJob]) {
      expect(verifyLabelToken(sign(p), SECRET, NOW)).toEqual({ ok: false, reason: "malformed" });
    }
  });
});

describe("guards", () => {
  it("refuses short secrets and non-uuid job ids when signing", () => {
    expect(() => signLabelToken({ jobId: JOB }, "short", NOW)).toThrow(/at least/);
    expect(() => verifyLabelToken("a.b", "short", NOW)).toThrow(/at least/);
    expect(() => signLabelToken({ jobId: "not-a-uuid" }, SECRET, NOW)).toThrow(/uuid/);
  });
  it("truncates very long display fields", () => {
    const token = signLabelToken({ jobId: JOB, filename: "x".repeat(1000), printer: "p".repeat(1000) }, SECRET, NOW);
    const r = verifyLabelToken(token, SECRET, NOW);
    expect(r.ok && r.claims.filename!.length).toBe(160);
    expect(r.ok && r.claims.printer!.length).toBe(80);
  });
});

describe("buildLabelUrl", () => {
  it("builds /label?t=<token> and tolerates a trailing slash", () => {
    expect(buildLabelUrl("https://site.example/", "a.b")).toBe("https://site.example/label?t=a.b");
    expect(buildLabelUrl("https://site.example", "a b")).toBe("https://site.example/label?t=a%20b");
  });
});

describe("feature flag + secrets", () => {
  it("is disabled unless EXACTLY 'true'", () => {
    expect(emailLabelingEnabled({})).toBe(false);
    for (const v of ["", "false", "1", "yes", "TRUE", " true"]) {
      expect(emailLabelingEnabled({ EMAIL_LABELING_ENABLED: v }), v).toBe(false);
    }
    expect(emailLabelingEnabled({ EMAIL_LABELING_ENABLED: "true" })).toBe(true);
  });
  it("labelLinkSecret requires >= 16 chars", () => {
    expect(labelLinkSecret({})).toBeNull();
    expect(labelLinkSecret({ LABEL_LINK_SECRET: "short" })).toBeNull();
    expect(labelLinkSecret({ LABEL_LINK_SECRET: SECRET })).toBe(SECRET);
  });
  it("safeEqual", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
  });
});

describe("resolveLabelLink — what the label page does with ?t=", () => {
  const env = (extra: Record<string, string> = {}): Env =>
    ({ EMAIL_LABELING_ENABLED: "true", LABEL_LINK_SECRET: SECRET, ...extra });
  const token = signLabelToken({ jobId: JOB, filename: "a.gcode.3mf", printer: "P" }, SECRET, NOW);

  it("is inert while the feature is off: a valid token changes nothing", () => {
    expect(resolveLabelLink(token, {}, NOW)).toEqual({ linked: null, problem: null });
    expect(resolveLabelLink(token, env({ EMAIL_LABELING_ENABLED: "false" }), NOW)).toEqual({ linked: null, problem: null });
  });
  it("is inert without a secret or without a token", () => {
    expect(resolveLabelLink(token, env({ LABEL_LINK_SECRET: "" }), NOW)).toEqual({ linked: null, problem: null });
    expect(resolveLabelLink(undefined, env(), NOW)).toEqual({ linked: null, problem: null });
  });
  it("links a valid token when enabled", () => {
    expect(resolveLabelLink(token, env(), NOW + 5)).toEqual({
      linked: { token, filename: "a.gcode.3mf", printer: "P" }, problem: null,
    });
  });
  it("explains expired and invalid links but keeps the form usable", () => {
    expect(resolveLabelLink(token, env(), NOW + LABEL_TOKEN_TTL_S + 1).problem).toMatch(/expired/);
    expect(resolveLabelLink("garbage", env(), NOW).problem).toMatch(/not valid/);
    expect(resolveLabelLink(token, env(), NOW + LABEL_TOKEN_TTL_S + 1).linked).toBeNull();
  });
});
