// station.ts — talk to the station's FastAPI server (printer status + camera).

export const STATION_API_URL =
  process.env.NEXT_PUBLIC_STATION_API_URL ??
  process.env.STATION_API_URL ??
  "";

export interface PrinterSnapshot {
  name: string;
  serial: string;
  ip: string;
  connected: boolean;
  state: string | null;
  subtask_name: string | null;
  gcode_file: string | null;
  nozzle_temper: number | null;
  bed_temper: number | null;
  filament_type: string | null;
  camera_url: string | null;
}

export async function fetchPrinters(): Promise<PrinterSnapshot[]> {
  if (!STATION_API_URL) return [];
  const res = await fetch(`${STATION_API_URL}/api/printers`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`station /api/printers failed: ${res.status}`);
  return (await res.json()) as PrinterSnapshot[];
}

export function cameraUrl(serial: string): string {
  return `${STATION_API_URL}/api/printer/${encodeURIComponent(serial)}/camera`;
}
