// /api/notify-print-end — FUTURE FEATURE (email reminder), DISABLED by default.
//
// Called by the lab station (x-station-key) when a student's print has ended.
// It builds a signed link to the label page and emails the student.
//
// Disabled unless EMAIL_LABELING_ENABLED=true: otherwise it answers 501 before
// doing anything. Even when enabled it refuses to run without LABEL_LINK_SECRET
// and an email provider. The recipient is DERIVED here (<netid>@EMAIL_DOMAIN):
// the station cannot choose an arbitrary address.

import { NextResponse } from "next/server";
import { buildLabelEmail, sendViaResend } from "@/lib/email";
import { isJobId } from "@/lib/labeling";
import {
  buildLabelUrl,
  emailLabelingEnabled,
  labelLinkSecret,
  safeEqual,
  signLabelToken,
} from "@/lib/labelLinks";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

// Cornell NetID shape: 2-4 letters then 1-5 digits (mirrors station/label_notifier.py).
const NETID_RE = /^[a-z]{2,4}[0-9]{1,5}$/;

export async function POST(req: Request) {
  if (!emailLabelingEnabled()) {
    return NextResponse.json({ error: "Email labeling is not enabled." }, { status: 501 });
  }

  const stationSecret = process.env.STATION_WEBHOOK_SECRET ?? "";
  if (!stationSecret || !safeEqual(req.headers.get("x-station-key") ?? "", stationSecret)) {
    return NextResponse.json({ error: "invalid station key" }, { status: 401 });
  }

  const linkSecret = labelLinkSecret();
  if (!linkSecret) {
    return NextResponse.json({ error: "LABEL_LINK_SECRET is not configured." }, { status: 501 });
  }
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.EMAIL_FROM;
  if (!apiKey || !from) {
    return NextResponse.json({ error: "Email provider is not configured." }, { status: 501 });
  }

  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }
  const netid = typeof body.netid === "string" ? body.netid.trim().toLowerCase() : "";
  const filename = typeof body.filename === "string" ? body.filename : "";
  const printerName = typeof body.printer_name === "string" ? body.printer_name : "";
  if (!isJobId(body.job_id) || !NETID_RE.test(netid) || !filename) {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }

  const token = signLabelToken(
    { jobId: body.job_id, filename, printer: printerName },
    linkSecret,
  );
  const base = process.env.PUBLIC_SITE_URL || new URL(req.url).origin;
  const email = buildLabelEmail({
    filename,
    printerName: printerName || "a MakerLAB printer",
    link: buildLabelUrl(base, token),
  });
  const to = `${netid}@${process.env.EMAIL_DOMAIN || "cornell.edu"}`;

  try {
    const result = await sendViaResend({ apiKey, from, to, ...email });
    if (!result.ok) {
      return NextResponse.json({ error: "Email provider refused the message." }, { status: 502 });
    }
  } catch {
    return NextResponse.json({ error: "Could not reach the email provider." }, { status: 502 });
  }
  return NextResponse.json({ ok: true });
}
