import { describe, expect, it } from "vitest";

import { IMAGE_BY_MODEL, printerImage, printerModel } from "./printerImages";

describe("printerModel", () => {
  it("recognises the X1 family", () => {
    expect(printerModel("Printer 1 — Bambu X1C")).toBe("x1c");
    expect(printerModel("Bambu X1 Carbon")).toBe("x1c");
    expect(printerModel("X1E")).toBe("x1c");
  });

  it("recognises the P1 family", () => {
    expect(printerModel("P1S")).toBe("p1s");
    expect(printerModel("Printer 4 — Bambu P1P")).toBe("p1s");
  });

  it("recognises the A1 family", () => {
    expect(printerModel("Bambu A1")).toBe("a1");
    expect(printerModel("A1 mini")).toBe("a1");
  });

  it("falls back to the generic illustration", () => {
    expect(printerModel("Printer 9 — Mystery")).toBe("generic");
    expect(printerModel("")).toBe("generic");
    expect(printerModel(null)).toBe("generic");
    expect(printerModel(undefined)).toBe("generic");
  });
});

describe("printerImage", () => {
  it("returns one shared image per model", () => {
    expect(printerImage("Bambu X1C")).toBe(IMAGE_BY_MODEL.x1c);
    expect(printerImage("Another X1C")).toBe(IMAGE_BY_MODEL.x1c);
    expect(printerImage("Bambu P1S")).toBe(IMAGE_BY_MODEL.p1s);
    expect(printerImage("Bambu A1")).toBe(IMAGE_BY_MODEL.a1);
    expect(printerImage("unknown")).toBe(IMAGE_BY_MODEL.generic);
  });
});
