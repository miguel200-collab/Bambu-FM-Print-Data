import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("STATION_API_URL", "https://station.example");
  vi.stubEnv("STATION_WEBHOOK_SECRET", "shh");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const get = (qs: string) => GET(new Request(`http://site/api/my-prints${qs}`));

describe("GET /api/my-prints", () => {
  it("400s without a usable NetID and never calls the station", async () => {
    for (const qs of ["", "?netid=", "?netid=%20%20", "?netid=!!!"]) {
      const res = await get(qs);
      expect(res.status, qs).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("normalises the NetID and attaches the station key server-side", async () => {
    fetchMock.mockResolvedValue(Response.json({ netid: "ab123", window_days: 14, prints: [] }));
    const res = await get("?netid=%20AB123%20");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ netid: "ab123", window_days: 14, prints: [] });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://station.example/api/my-prints?netid=ab123");
    expect(init.headers["x-station-key"]).toBe("shh");
  });

  it("the station key is never part of the response sent to the browser", async () => {
    fetchMock.mockResolvedValue(Response.json({ netid: "ab123", window_days: 14, prints: [] }));
    const text = await (await get("?netid=ab123")).text();
    expect(text).not.toContain("shh");
  });

  it("relays a station 4xx message and maps a station 5xx to 502", async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ detail: "netid is required" }, { status: 400 }));
    let res = await get("?netid=ab123");
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBe("netid is required");

    fetchMock.mockResolvedValueOnce(new Response("boom", { status: 500 }));
    res = await get("?netid=ab123");
    expect(res.status).toBe(502);
  });

  it("says the station is offline when it cannot be reached or is not configured", async () => {
    fetchMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    let res = await get("?netid=ab123");
    expect(res.status).toBe(503);
    expect((await res.json()).error).toMatch(/offline/);

    vi.stubEnv("STATION_API_URL", "");
    res = await get("?netid=ab123");
    expect(res.status).toBe(503);
  });
});
