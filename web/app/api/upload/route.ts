// /api/upload — receives a student submission and stores it in Vercel Blob
// under "incoming/<name>__<targetSerial>__<originalFilename>". The station's
// file_watcher polls that prefix, downloads the file, renames it to
// "<name>_<filename>.gcode.3mf", and uploads it to the chosen printer.

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
    return NextResponse.json({ ok: true, url: blob.url, pathname: blob.pathname });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to store file.";
    return NextResponse.json({ ok: false, error: message }, { status: 500 });
  }
}
