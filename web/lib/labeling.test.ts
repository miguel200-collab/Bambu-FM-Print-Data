import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  FAILURE_DEFINITION,
  isJobId,
  isOutcome,
  normalizeNetId,
  outcomeFromLabel,
} from "./labeling";

// The SAME cases are asserted in Python (tests/test_matching.py), so the web and
// the station can never disagree about which student a NetID refers to.
const fixture = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/netid_cases.json", import.meta.url), "utf-8"),
) as { cases: [string, string][] };

describe("normalizeNetId", () => {
  it("matches the shared Python fixture", () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(10);
    for (const [raw, expected] of fixture.cases) {
      expect(normalizeNetId(raw), JSON.stringify(raw)).toBe(expected === "" ? null : expected);
    }
  });
  it("rejects non-strings", () => {
    expect(normalizeNetId(undefined)).toBeNull();
    expect(normalizeNetId(null)).toBeNull();
    expect(normalizeNetId(123)).toBeNull();
    expect(normalizeNetId({})).toBeNull();
  });
});

describe("isJobId", () => {
  it("accepts a uuid4 and rejects everything else", () => {
    expect(isJobId("123e4567-e89b-42d3-a456-426614174000")).toBe(true);
    expect(isJobId("123E4567-E89B-42D3-A456-426614174000")).toBe(true);
    for (const bad of ["", "nope", "123e4567e89b42d3a456426614174000", "../../etc/passwd", 7, null, undefined]) {
      expect(isJobId(bad), String(bad)).toBe(false);
    }
  });
});

describe("outcomes", () => {
  it("isOutcome", () => {
    expect(isOutcome("succeeded")).toBe(true);
    expect(isOutcome("failed")).toBe(true);
    expect(isOutcome("maybe")).toBe(false);
    expect(isOutcome(0)).toBe(false);
  });
  it("outcomeFromLabel maps 0/1/null", () => {
    expect(outcomeFromLabel(0)).toBe("succeeded");
    expect(outcomeFromLabel(1)).toBe("failed");
    expect(outcomeFromLabel(null)).toBeNull();
  });
  it("the failure definition is a single sentence students can read", () => {
    expect(FAILURE_DEFINITION).toMatch(/did not complete/);
    expect(FAILURE_DEFINITION).toMatch(/unusable/);
    expect(FAILURE_DEFINITION.split(".").filter(Boolean)).toHaveLength(1);
  });
});
