// /api/upload — receives a student submission and stores it in Vercel Blob
// under "incoming/<name>__<targetSerial>__<originalFilename>", then notifies the
// dedicated laptop (via its /api/blob-webhook endpoint over the Cloudflare
// Tunnel) so it can download and process the file without polling Blob.
//
// The notify step is best-effort: if the laptop/tunnel is unreachable the blob
// still sits safely in Blob, and the laptop's startup catch-up list will grab it
// the next time it boots. So a failed notify never fails the student's upload.

import { NextResponse } from "next/server";
import { putSubmission } from "@/lib/blob";

export const runtime = "nodejs";

const ALLOWED_EXT = [".gcode.3mf", ".3mf.gcode", ".3mf", ".gcode"];

function normalizeExt(name: string): string {
  const lower = name.toLowerCase();
  for (const ext of ALLOWED_EXT) {
    if (lower.endsWith(ext)) {
      return name.slice(0, name.length - ext.length) + ".gcode.3mf";
    }
  }
  return name;
}

async function notifyStation(pathname: string, url: string): Promise<void> {
  const stationApiUrl = process.env.STATION_API_URL;
  if (!stationApiUrl) {
    // No station configured yet — blob stays in Blob until the laptop catches up.
    return;
  }
  const endpoint = `${stationApiUrl.replace(/\/$/, "")}/api/blob-webhook`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-station-key": process.env.STATION_WEBHOOK_SECRET ?? "",
      },
      body: JSON.stringify({ pathname, url }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      console.warn(`station notify returned HTTP ${resp.status} for ${pathname}`);
    }
  } catch (err) {
    // Tunnel down / laptop offline — not fatal. Blob is buffered in Blob.
    console.warn("station notify failed (will catch up on startup):", err);
  } finally {
    clearTimeout(timeout);
  }
}

export async function POST(req: Request) {
  const formData = await req.formData();
  const studentName = (formData.get("studentName") as string | null)?.trim();
  const targetPrinter = (formData.get("targetPrinter") as string | null) || "any";
  const file = formData.get("file") as File | null;

  if (!studentName) {
    return NextResponse.json({ ok: false, error: "Name is required." }, { status: 400 });
  }
  if (!file || file.size === 0) {
    return NextResponse.json({ ok: false, error: "File is required." }, { status: 400 });
  }

  const filename = normalizeExt(file.name);
  const lower = filename.toLowerCase();
  if (!ALLOWED_EXT.some((ext) => lower.endsWith(ext))) {
    return NextResponse.json(
      { ok: false, error: "File must be a .gcode.3mf slice." },
      { status: 400 },
    );
  }

  try {
    const blob = await putSubmission({
      studentName,
      targetPrinter,
      filename,
      file,
    });
    // Best-effort: tell the laptop to come grab this blob now. Never let a
    // notify failure fail the upload — the blob is durably stored either way.
    await notifyStation(blob.pathname, blob.url);
    return NextResponse.json({ ok: true, url: blob.url, pathname: blob.pathname });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to store file.";
    return NextResponse.json({ ok: false, error: message }, { status: 500 });
  }
}
