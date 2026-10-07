import { describe, expect, it, vi } from "vitest";
import { buildLabelEmail, escapeHtml, sendViaResend } from "./email";
import { FAILURE_DEFINITION } from "./labeling";

describe("escapeHtml", () => {
  it("escapes the five dangerous characters", () => {
    expect(escapeHtml(`<a href="x" onclick='y'>&`)).toBe("&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;");
  });
});

describe("buildLabelEmail", () => {
  const input = { filename: "ab123_bracket.gcode.3mf", printerName: "The Allee", link: "https://site.example/label?t=a.b" };

  it("contains the link, the file, the printer and the failure definition", () => {
    const e = buildLabelEmail(input);
    for (const part of [input.link, input.filename, input.printerName, FAILURE_DEFINITION]) {
      expect(e.text).toContain(part);
    }
    expect(e.html).toContain(`href="${input.link}"`);
    expect(e.subject).toMatch(/print turn out/);
  });

  it("neutralises HTML in student-controlled filenames and printer names", () => {
    const e = buildLabelEmail({
      filename: `"><script>alert(1)</script>.gcode.3mf`,
      printerName: "<img src=x onerror=alert(1)>",
      link: "https://site.example/label?t=a&b=c",
    });
    expect(e.html).not.toContain("<script>");
    expect(e.html).not.toContain("<img");
    expect(e.html).toContain("&lt;script&gt;");
    expect(e.html).toContain("t=a&amp;b=c");     // the link is escaped inside the attribute too
  });
});

describe("sendViaResend", () => {
  it("POSTs to Resend with a bearer key and a single recipient", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    const out = await sendViaResend(
      { apiKey: "re_key", from: "MakerLAB <m@x.edu>", to: "ab123@cornell.edu", subject: "s", text: "t", html: "<p>h</p>" },
      fetchImpl as unknown as typeof fetch,
    );
    expect(out).toEqual({ ok: true, status: 200 });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("https://api.resend.com/emails");
    expect(init.headers.authorization).toBe("Bearer re_key");
    expect(JSON.parse(init.body)).toEqual({
      from: "MakerLAB <m@x.edu>", to: ["ab123@cornell.edu"], subject: "s", text: "t", html: "<p>h</p>",
    });
  });

  it("reports a refusal without throwing", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(new Response("no", { status: 422 }));
    expect(await sendViaResend(
      { apiKey: "k", from: "f", to: "t", subject: "s", text: "t", html: "h" },
      fetchImpl as unknown as typeof fetch,
    )).toEqual({ ok: false, status: 422 });
  });
});
