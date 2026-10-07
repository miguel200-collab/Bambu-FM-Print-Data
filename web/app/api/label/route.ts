// /api/label — server-side proxy to the station's authenticated
// POST /api/label-webhook. Records a student's human label for one print.
//
// The browser sends {netid, job_id, outcome}; this route validates the shape,
// adds source="web" and the station key, and relays the station's verdict
// (the station checks that the print really belongs to that NetID and that it
// ended within the 14-day labeling window).

import { NextResponse } from "next/server";
import {
  isJobId,
  isOutcome,
  normalizeNetId,
  STATION_OFFLINE_MESSAGE,
} from "@/lib/labeling";
import {
  StationUnavailableError,
  stationErrorMessage,
  stationFetch,
} from "@/lib/stationServer";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function POST(req: Request) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }
  const { netid: rawNetid, job_id, outcome } = (body ?? {}) as Record<string, unknown>;

  const netid = normalizeNetId(rawNetid);
  if (!netid) return NextResponse.json({ error: "Enter your NetID." }, { status: 400 });
  if (!isJobId(job_id)) return NextResponse.json({ error: "Invalid print." }, { status: 400 });
  if (!isOutcome(outcome)) return NextResponse.json({ error: "Invalid outcome." }, { status: 400 });

  try {
    const res = await stationFetch("/api/label-webhook", {
      method: "POST",
      body: { job_id, outcome, source: "web", netid },
    });
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
