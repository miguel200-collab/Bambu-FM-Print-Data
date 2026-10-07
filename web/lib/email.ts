// email.ts — compose and send the "how did your print turn out?" email.
// FUTURE FEATURE: only reachable when EMAIL_LABELING_ENABLED=true.
//
// Provider: Resend (https://resend.com/docs/api-reference/emails/send-email).
// `fetchImpl` is injectable so tests never touch the network.

import { FAILURE_DEFINITION } from "./labeling";

export interface LabelEmailInput {
  filename: string;
  printerName: string;
  link: string;
}

export interface EmailContent {
  subject: string;
  text: string;
  html: string;
}

// Filenames and printer names are student-controlled: ALWAYS escape before they
// go into HTML.
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function buildLabelEmail({ filename, printerName, link }: LabelEmailInput): EmailContent {
  const subject = "How did your MakerLAB print turn out?";
  const text = [
    "Your print has finished.",
    "",
    `File: ${filename}`,
    `Printer: ${printerName}`,
    "",
    "Tell us how it turned out (it takes ten seconds):",
    link,
    "",
    FAILURE_DEFINITION,
    "",
    "The link works for 14 days. You are receiving this single email because you agreed to it when you submitted your file.",
  ].join("\n");

  const html = `<p>Your print has finished.</p>
<p><strong>File:</strong> ${escapeHtml(filename)}<br><strong>Printer:</strong> ${escapeHtml(printerName)}</p>
<p><a href="${escapeHtml(link)}">Tell us how it turned out</a> (ten seconds).</p>
<p>${escapeHtml(FAILURE_DEFINITION)}</p>
<p style="color:#666;font-size:12px">The link works for 14 days. You are receiving this single email because you agreed to it when you submitted your file.</p>`;
  return { subject, text, html };
}

export interface SendInput extends EmailContent {
  apiKey: string;
  from: string;
  to: string;
}

export async function sendViaResend(
  { apiKey, from, to, subject, text, html }: SendInput,
  fetchImpl: typeof fetch = fetch,
): Promise<{ ok: boolean; status: number }> {
  const res = await fetchImpl("https://api.resend.com/emails", {
    method: "POST",
    headers: { authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
    body: JSON.stringify({ from, to: [to], subject, text, html }),
  });
  return { ok: res.ok, status: res.status };
}
