// /api/upload — client-side upload route for student submissions.
//
// Uses Vercel Blob's `handleUpload` to issue short-lived client tokens so the
// browser uploads the .gcode.3mf file directly to Vercel Blob — the file never
// passes through this function, so Vercel's 4.5 MB serverless body limit does
// not apply. When the upload completes, Vercel Blob calls back into this route
// (onUploadCompleted), and we best-effort POST {pathname, downloadUrl} to the
// dedicated laptop's /api/blob-webhook (over the Cloudflare Tunnel) so it
// downloads the blob immediately — webhook-driven, no polling.
//
// The notify step is best-effort: if the laptop/tunnel is unreachable the blob
// still sits safely in Blob, and the laptop's startup catch-up list will grab
// it the next time it boots. A failed notify never fails the student's upload.

import { NextResponse } from "next/server";
import { handleUpload } from "@vercel/blob/client";
import { EXPORT_HINT, isAllowedFilename, parsePathname } from "@/lib/blob";

export const runtime = "nodejs";

const MAX_SIZE = 250 * 1024 * 1024; // 250 MB cap

async function notifyStation(pathname: string, url: string, downloadUrl?: string): Promise<void> {
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
      body: JSON.stringify({ pathname, url, downloadUrl }),
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
  const body = await req.json();

  try {
    const result = await handleUpload({
      body,
      request: req,
      onBeforeGenerateToken: async (pathname, _clientPayload, _multipart) => {
        const parsed = parsePathname(pathname);
        if (!parsed || !parsed.name || parsed.name === "anon") {
          throw new Error("Name is required.");
        }
        if (!isAllowedFilename(parsed.file)) {
          throw new Error(`File must be a sliced .gcode.3mf. ${EXPORT_HINT}`);
        }
        return {
          maximumSizeInBytes: MAX_SIZE,
          addRandomSuffix: false,
          allowOverwrite: false,
        };
      },
      onUploadCompleted: async ({ blob }) => {
        // Private store: blob.url is not fetchable without auth, so forward the
        // signed downloadUrl (valid up to 7 days) for the laptop to GET. Keep
        // blob.url too — the laptop uses it for the authenticated DELETE.
        await notifyStation(blob.pathname, blob.url, blob.downloadUrl);
      },
    });
    return NextResponse.json(result);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to handle upload.";
    return NextResponse.json({ ok: false, error: message }, { status: 400 });
  }
}
