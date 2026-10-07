// /api/printers — same-origin proxy to the station's /api/printers so the
// browser can poll live status without needing NEXT_PUBLIC_STATION_API_URL
// or CORS on the station. Returns [] (200) when the station is unreachable,
// matching the page's offline handling.

import { NextResponse } from "next/server";
import { fetchPrinters } from "@/lib/station";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const printers = await fetchPrinters();
    return NextResponse.json(printers);
  } catch {
    return NextResponse.json([]);
  }
}
