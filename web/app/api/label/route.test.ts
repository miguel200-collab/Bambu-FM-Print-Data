import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "./route";

const fetchMock = vi.fn();
const JOB = "123e4567-e89b-42d3-a456-426614174000";

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

const post = (body: unknown) =>
  POST(new Request("http://site/api/label", { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body) }));

describe("POST /api/label — validation", () => {
  it("rejects malformed JSON, missing NetID, bad job ids and bad outcomes without calling the station", async () => {
    const bad: unknown[] = [
      "not json",
      {},
      { netid: "ab123", job_id: JOB },                                         // no outcome
      { netid: "", job_id: JOB, outcome: "failed" },
      { netid: "ab123", job_id: "../../etc/passwd", outcome: "failed" },
      { netid: "ab123", job_id: JOB, outcome: "maybe" },
      { netid: "ab123", job_id: JOB, outcome: 1 },
    ];
    for (const body of bad) {
      const res = await post(body);
      expect(res.status, JSON.stringify(body)).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/label — proxy", () => {
  it("forwards a normalised NetID with source=web and the station key", async () => {
    fetchMock.mockResolvedValue(Response.json({ ok: true, job_id: JOB, label: 1, source: "web" }));
    const res = await post({ netid: " AB123 ", job_id: JOB, outcome: "failed" });
    expect(res.status).toBe(200);
    expect((await res.json()).label).toBe(1);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://station.example/api/label-webhook");
    expect(init.method).toBe("POST");
    expect(init.headers["x-station-key"]).toBe("shh");
    expect(JSON.parse(init.body)).toEqual({ job_id: JOB, outcome: "failed", source: "web", netid: "ab123" });
  });

  it("ignores a client-supplied source: a browser can never claim to be an email click", async () => {
    fetchMock.mockResolvedValue(Response.json({ ok: true }));
    await post({ netid: "ab123", job_id: JOB, outcome: "succeeded", source: "email", era: 2, label: 0 });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      job_id: JOB, outcome: "succeeded", source: "web", netid: "ab123",
    });
  });

  it("relays station refusals so the student knows why", async () => {
    const cases: [number, string][] = [
      [403, "that print is not yours"],
      [404, "unknown print"],
      [409, "that print has not finished yet"],
      [410, "labeling closes 14 days after a print ends"],
    ];
    for (const [status, detail] of cases) {
      fetchMock.mockResolvedValueOnce(Response.json({ detail }, { status }));
      const res = await post({ netid: "ab123", job_id: JOB, outcome: "failed" });
      expect(res.status).toBe(status);
      expect((await res.json()).error).toBe(detail);
    }
  });

  it("maps station 5xx to 502 and an unreachable station to 503", async () => {
    fetchMock.mockResolvedValueOnce(new Response("x", { status: 500 }));
    expect((await post({ netid: "ab123", job_id: JOB, outcome: "failed" })).status).toBe(502);
    fetchMock.mockRejectedValueOnce(new Error("down"));
    const res = await post({ netid: "ab123", job_id: JOB, outcome: "failed" });
    expect(res.status).toBe(503);
    expect((await res.json()).error).toMatch(/offline/);
  });
});
