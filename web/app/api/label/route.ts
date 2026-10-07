// /api/label — server-side proxy to the station's authenticated
// POST /api/label-webhook. Records a student's human label for one print.
//
// Two ways in, both validated here and relayed with source set BY THIS ROUTE
// (never taken from the client):
//
//   1. Website:  {netid, job_id, outcome}  -> source="web". The station checks
//      that the print really belongs to that NetID and ended within 14 days.
//   2. Email link (FUTURE, disabled unless EMAIL_LABELING_ENABLED=true):
//      {token, outcome} -> the signed token identifies the print -> source="email".

import { NextResponse } from "next/server";
import {
  isJobId,
  isOutcome,
  normalizeNetId,
  STATION_OFFLINE_MESSAGE,
} from "@/lib/labeling";
import { emailLabelingEnabled, labelLinkSecret, verifyLabelToken } from "@/lib/labelLinks";
import {
  StationUnavailableError,
  stationErrorMessage,
  stationFetch,
} from "@/lib/stationServer";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

async function forwardToStation(payload: Record<string, unknown>): Promise<Response> {
  try {
    const res = await stationFetch("/api/label-webhook", { method: "POST", body: payload });
    if (!res.ok) {
      const message = await stationErrorMessage(res, "Could not save your label.");
      return NextResponse.json(
        { error: message },
        { status: res.status >= 400 && res.status < 500 ? res.status : 502 },
      );
    }
    return NextResponse.json(await res.json());
  } catch (err) {
    if (err instanceof StationUnavailableError) {
      return NextResponse.json({ error: STATION_OFFLINE_MESSAGE }, { status: 503 });
    }
    return NextResponse.json({ error: "Could not save your label." }, { status: 500 });
  }
}

export async function POST(req: Request) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }
  const { netid: rawNetid, job_id, outcome, token } = (body ?? {}) as Record<string, unknown>;

  if (!isOutcome(outcome)) {
    return NextResponse.json({ error: "Invalid outcome." }, { status: 400 });
  }

  // ---- email link (future, disabled by default) ----
  if (token !== undefined) {
    const secret = labelLinkSecret();
    if (!emailLabelingEnabled() || !secret) {
      return NextResponse.json({ error: "Email labeling is not enabled." }, { status: 501 });
    }
    const verified = verifyLabelToken(token, secret);
    if (!verified.ok) {
      const status = verified.reason === "expired" ? 410 : verified.reason === "malformed" ? 400 : 403;
      const error =
        verified.reason === "expired"
          ? "This link has expired. You can still label your print from the Label page."
          : "This link is not valid.";
      return NextResponse.json({ error }, { status });
    }
    return forwardToStation({ job_id: verified.claims.jobId, outcome, source: "email" });
  }

  // ---- website ----
  const netid = normalizeNetId(rawNetid);
  if (!netid) return NextResponse.json({ error: "Enter your NetID." }, { status: 400 });
  if (!isJobId(job_id)) return NextResponse.json({ error: "Invalid print." }, { status: 400 });

  return forwardToStation({ job_id, outcome, source: "web", netid });
}
