import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// handleUpload normally talks to Vercel Blob. The mock just runs the route's own
// callbacks so we test OUR validation, not the SDK.
const captured: { opts?: any } = {};
vi.mock("@vercel/blob/client", () => ({
  handleUpload: vi.fn(async (opts: any) => {
    captured.opts = opts;
    const cfg = await opts.onBeforeGenerateToken(opts.body.pathname, null, false);
    return { type: "blob.generate-client-token", cfg };
  }),
}));

import { POST } from "./route";

function post(pathname: string) {
  return POST(new Request("http://site/api/upload", { method: "POST", body: JSON.stringify({ pathname }) }));
}

describe("/api/upload — onBeforeGenerateToken", () => {
  it("accepts a sliced .gcode.3mf with a name and caps the size at 250 MB", async () => {
    const res = await post("incoming/ab123__any__bracket.gcode.3mf");
    expect(res.status).toBe(200);
    const { cfg } = await res.json();
    expect(cfg.maximumSizeInBytes).toBe(250 * 1024 * 1024);
    expect(cfg.allowOverwrite).toBe(false);
    expect(cfg.addRandomSuffix).toBe(false);
  });

  it.each([
    ["a plain .3mf model", "incoming/ab123__any__model.3mf"],
    ["a plain .gcode", "incoming/ab123__any__print.gcode"],
    ["the old .3mf.gcode form", "incoming/ab123__any__print.3mf.gcode"],
    ["an unrelated file", "incoming/ab123__any__virus.exe"],
    ["an empty stem", "incoming/ab123__any__.gcode.3mf"],
  ])("rejects %s with a helpful message", async (_label, pathname) => {
    const res = await post(pathname);
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.ok).toBe(false);
    expect(body.error).toMatch(/sliced \.gcode\.3mf/);
    expect(body.error).toMatch(/Export plate sliced file/);
  });

  it("requires a student name", async () => {
    for (const pathname of ["incoming/anon__any__f.gcode.3mf", "incoming/f.gcode.3mf", "garbage"]) {
      const res = await post(pathname);
      expect(res.status, pathname).toBe(400);
      expect((await res.json()).error).toBe("Name is required.");
    }
  });
});

describe("/api/upload — onUploadCompleted notifies the station", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("STATION_API_URL", "https://station.example/");
    vi.stubEnv("STATION_WEBHOOK_SECRET", "shh");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  async function complete() {
    await post("incoming/ab123__any__bracket.gcode.3mf"); // populates captured.opts
    await captured.opts.onUploadCompleted({
      blob: { pathname: "incoming/ab123__any__bracket.gcode.3mf", url: "https://blob/x", downloadUrl: "https://blob/x?dl" },
    });
  }

  it("POSTs pathname/url/downloadUrl with the shared secret", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));
    await complete();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://station.example/api/blob-webhook");   // trailing slash handled
    expect(init.headers["x-station-key"]).toBe("shh");
    expect(JSON.parse(init.body)).toEqual({
      pathname: "incoming/ab123__any__bracket.gcode.3mf",
      url: "https://blob/x",
      downloadUrl: "https://blob/x?dl",
    });
  });

  it("never fails the student's upload when the laptop is unreachable", async () => {
    fetchMock.mockRejectedValue(new Error("tunnel down"));
    await expect(complete()).resolves.toBeUndefined();
  });

  it("does nothing when no station is configured", async () => {
    vi.stubEnv("STATION_API_URL", "");
    await complete();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
