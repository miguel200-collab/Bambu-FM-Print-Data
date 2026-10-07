import { describe, expect, it } from "vitest";
import {
  ALLOWED_EXT,
  buildPathname,
  hasZipSignature,
  isAllowedFilename,
  normalizeExt,
  parsePathname,
  sanitize,
} from "./blob";

describe("sanitize", () => {
  it("keeps safe characters and replaces the rest", () => {
    expect(sanitize("ab123")).toBe("ab123");
    expect(sanitize("My Part (1).gcode.3mf")).toBe("My_Part_1_.gcode.3mf");
    expect(sanitize("a/b\\c")).toBe("a_b_c");
  });
  it("trims and falls back to anon", () => {
    expect(sanitize("  ab123  ")).toBe("ab123");
    expect(sanitize("")).toBe("anon");
    expect(sanitize("***")).toBe("anon");
  });
});

describe("isAllowedFilename — only a sliced .gcode.3mf", () => {
  it("accepts .gcode.3mf in any case", () => {
    expect(isAllowedFilename("part.gcode.3mf")).toBe(true);
    expect(isAllowedFilename("PART.GCODE.3MF")).toBe(true);
    expect(isAllowedFilename("my.part.v2.gcode.3mf")).toBe(true);
  });
  it("rejects a plain .3mf model, a plain .gcode and the old alternatives", () => {
    // These were all accepted before (and force-renamed), causing BFM "file error".
    for (const bad of ["part.3mf", "part.gcode", "part.3mf.gcode", "part.stl", "part", "part.gcode.3mf.zip"]) {
      expect(isAllowedFilename(bad), bad).toBe(false);
    }
  });
  it("rejects an empty stem", () => {
    expect(isAllowedFilename(".gcode.3mf")).toBe(false);
    expect(isAllowedFilename("")).toBe(false);
  });
  it("allows exactly one extension", () => {
    expect(ALLOWED_EXT).toEqual([".gcode.3mf"]);
  });
});

describe("normalizeExt", () => {
  it("canonicalises extension case", () => {
    expect(normalizeExt("Part.GCODE.3MF")).toBe("Part.gcode.3mf");
    expect(normalizeExt("part.gcode.3mf")).toBe("part.gcode.3mf");
  });
  it("never silently renames other file types (regression: plain .3mf -> .gcode.3mf)", () => {
    expect(normalizeExt("model.3mf")).toBe("model.3mf");
    expect(normalizeExt("model.gcode")).toBe("model.gcode");
  });
});

describe("buildPathname / parsePathname", () => {
  it("builds incoming/<name>__<target>__<file>", () => {
    expect(buildPathname("ab123", "SERIAL1", "bracket.gcode.3mf")).toBe(
      "incoming/ab123__SERIAL1__bracket.gcode.3mf",
    );
  });
  it("defaults the target to any and sanitises every part", () => {
    expect(buildPathname(" ab 123 ", "", "My Part.gcode.3mf")).toBe(
      "incoming/ab_123__any__My_Part.gcode.3mf",
    );
  });
  it("round-trips through parsePathname", () => {
    const p = parsePathname(buildPathname("ab123", "any", "x y.gcode.3mf"));
    expect(p).toEqual({ name: "ab123", target: "any", file: "x_y.gcode.3mf" });
    expect(isAllowedFilename(p!.file)).toBe(true);
  });
  it("parses with and without the incoming/ prefix and a missing target", () => {
    expect(parsePathname("incoming/a__b.gcode.3mf")).toEqual({ name: "a", target: "any", file: "b.gcode.3mf" });
    expect(parsePathname("a__T__f.gcode.3mf")).toEqual({ name: "a", target: "T", file: "f.gcode.3mf" });
  });
  it("returns null when there is no <name>__<file>", () => {
    expect(parsePathname("incoming/justonepart.gcode.3mf")).toBeNull();
    expect(parsePathname("")).toBeNull();
  });
  it("a filename containing __ still validates on its extension", () => {
    const p = parsePathname("incoming/a__any__my__file.gcode.3mf");
    expect(isAllowedFilename(p!.file)).toBe(true);
  });
});

describe("hasZipSignature", () => {
  it("accepts a PK\\x03\\x04 header", () => {
    expect(hasZipSignature(new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00]))).toBe(true);
  });
  it("rejects plain G-code, short and empty input", () => {
    expect(hasZipSignature(new TextEncoder().encode("; HEADER_BLOCK"))).toBe(false);
    expect(hasZipSignature(new Uint8Array([0x50, 0x4b]))).toBe(false);
    expect(hasZipSignature(new Uint8Array([]))).toBe(false);
    expect(hasZipSignature([0x50, 0x4b, 0x05, 0x06])).toBe(false); // empty-zip signature
  });
});
