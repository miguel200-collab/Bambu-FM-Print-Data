// /api/my-prints — server-side proxy to the station's authenticated
// GET /api/my-prints. The browser sends only a NetID; the station key is
// attached here, so it never reaches a student's browser.
//
// NOTE: typing a NetID is not authentication — anyone who knows a NetID can list
// that student's recent print filenames. Accepted for v1 (see AGENTS.md); the
// email flow's signed links replace NetID entry later.

import { NextResponse } from "next/server";
import { normalizeNetId, STATION_OFFLINE_MESSAGE } from "@/lib/labeling";
import {
  StationUnavailableError,
  stationErrorMessage,
  stationFetch,
} from "@/lib/stationServer";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: Request) {
  const netid = normalizeNetId(new URL(req.url).searchParams.get("netid"));
  if (!netid) {
    return NextResponse.json({ error: "Enter your NetID." }, { status: 400 });
  }

  try {
    const res = await stationFetch(`/api/my-prints?netid=${encodeURIComponent(netid)}`);
    if (!res.ok) {
      const message = await stationErrorMessage(res, "Could not load your prints.");
      // 4xx from the station is the student's problem; anything else is ours.
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
    return NextResponse.json({ error: "Could not load your prints." }, { status: 500 });
  }
}
