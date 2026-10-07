// stationServer.ts — SERVER-ONLY helper for calling the station's authenticated
// endpoints (/api/my-prints, /api/label-webhook).
//
// It attaches the shared secret (x-station-key). Never import this file from a
// client component: the secret must stay on the Vercel server.

export class StationUnavailableError extends Error {}

export async function stationFetch(
  path: string,
  init: { method?: string; body?: unknown } = {},
  timeoutMs = 8000,
): Promise<Response> {
  const base = process.env.STATION_API_URL;
  if (!base) throw new StationUnavailableError("STATION_API_URL is not configured");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${base.replace(/\/$/, "")}${path}`, {
      method: init.method ?? "GET",
      headers: {
        "content-type": "application/json",
        "x-station-key": process.env.STATION_WEBHOOK_SECRET ?? "",
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (err) {
    throw new StationUnavailableError(
      err instanceof Error ? err.message : "station unreachable",
    );
  } finally {
    clearTimeout(timer);
  }
}

// Station errors look like {"detail": "..."} (FastAPI). Pull out a message safe
// to show a student, falling back to a generic one.
export async function stationErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail) return data.detail;
  } catch {
    /* body was not JSON */
  }
  return fallback;
}
