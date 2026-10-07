import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { verifyLabelToken } from "@/lib/labelLinks";
import { POST } from "./route";

const fetchMock = vi.fn();
const JOB = "123e4567-e89b-42d3-a456-426614174000";
const SECRET = "0123456789abcdef-secret";

function enable() {
  vi.stubEnv("EMAIL_LABELING_ENABLED", "true");
  vi.stubEnv("STATION_WEBHOOK_SECRET", "shh");
  vi.stubEnv("LABEL_LINK_SECRET", SECRET);
  vi.stubEnv("RESEND_API_KEY", "re_key");
  vi.stubEnv("EMAIL_FROM", "MakerLAB <m@x.edu>");
  vi.stubEnv("PUBLIC_SITE_URL", "https://site.example");
}

const good = { job_id: JOB, netid: "ab123", filename: "ab123_bracket.gcode.3mf", printer_name: "The Allee" };

function post(body: unknown, headers: Record<string, string> = { "x-station-key": "shh" }) {
  return POST(new Request("http://site/api/notify-print-end", {
    method: "POST", headers, body: typeof body === "string" ? body : JSON.stringify(body),
  }));
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("DISABLED by default", () => {
  it("answers 501 and does nothing, even with a perfect request", async () => {
    vi.stubEnv("STATION_WEBHOOK_SECRET", "shh");
    vi.stubEnv("LABEL_LINK_SECRET", SECRET);
    vi.stubEnv("RESEND_API_KEY", "re_key");
    vi.stubEnv("EMAIL_FROM", "m@x.edu");
    const res = await post(good);                           // EMAIL_LABELING_ENABLED not set
    expect(res.status).toBe(501);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["false", "", "1", "TRUE"])("stays disabled for EMAIL_LABELING_ENABLED=%j", async (v) => {
    enable();
    vi.stubEnv("EMAIL_LABELING_ENABLED", v);
    expect((await post(good)).status).toBe(501);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("when enabled", () => {
  beforeEach(enable);

  it("requires the station key", async () => {
    const attempts: Record<string, string>[] = [{}, { "x-station-key": "wrong" }, { "x-station-key": "" }];
    for (const headers of attempts) {
      expect((await post(good, headers)).status).toBe(401);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("501s when the link secret or the provider is not configured", async () => {
    vi.stubEnv("LABEL_LINK_SECRET", "short");
    expect((await post(good)).status).toBe(501);
    enable();
    vi.stubEnv("RESEND_API_KEY", "");
    expect((await post(good)).status).toBe(501);
    enable();
    vi.stubEnv("EMAIL_FROM", "");
    expect((await post(good)).status).toBe(501);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects malformed bodies", async () => {
    const bad: unknown[] = [
      "not json", {}, { ...good, job_id: "nope" }, { ...good, netid: "Miguel Ramirez" },
      { ...good, netid: "evil@attacker.com" }, { ...good, netid: "123456" }, { ...good, filename: "" },
    ];
    for (const body of bad) {
      expect((await post(body)).status, JSON.stringify(body)).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sends ONE email to <netid>@cornell.edu with a verifiable link to the label page", async () => {
    const res = await post(good);
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.resend.com/emails");
    const sent = JSON.parse(init.body);
    expect(sent.to).toEqual(["ab123@cornell.edu"]);
    expect(sent.from).toBe("MakerLAB <m@x.edu>");

    const link = /https:\/\/site\.example\/label\?t=([A-Za-z0-9_.-]+)/.exec(sent.text);
    expect(link).not.toBeNull();
    const verified = verifyLabelToken(link![1], SECRET);
    expect(verified.ok && verified.claims.jobId).toBe(JOB);
    expect(verified.ok && verified.claims.filename).toBe(good.filename);
  });

  it("derives the recipient itself: a client-supplied email/to is ignored", async () => {
    await post({ ...good, email: "attacker@evil.com", to: "attacker@evil.com" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).to).toEqual(["ab123@cornell.edu"]);
  });

  it("honours EMAIL_DOMAIN and lower-cases the NetID", async () => {
    vi.stubEnv("EMAIL_DOMAIN", "example.edu");
    await post({ ...good, netid: "AB123" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).to).toEqual(["ab123@example.edu"]);
  });

  it("maps a provider refusal or outage to 502", async () => {
    fetchMock.mockResolvedValueOnce(new Response("no", { status: 403 }));
    expect((await post(good)).status).toBe(502);
    fetchMock.mockRejectedValueOnce(new Error("network"));
    expect((await post(good)).status).toBe(502);
  });
});
