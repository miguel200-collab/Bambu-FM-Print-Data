// blob.ts — helpers for student submission pathnames + Vercel Blob writes.
//
// The pathname encodes everything the station's file_watcher needs to route the
// file, so no separate metadata store is required:
//   incoming/<safeName>__<targetSerial>__<originalFilename>
// where targetSerial is "any" if the student didn't pick a specific printer.
// The filename is normalized to a .gcode.3mf extension.
//
// With client-side uploads (handleUpload/upload from @vercel/blob/client), the
// browser builds the pathname and uploads the file directly to Vercel Blob; the
// server only validates the pathname in onBeforeGenerateToken. putSubmission
// (server-side put) is kept as a fallback but is no longer used on the main path.

import { put } from "@vercel/blob";

const ALLOWED_EXT = [".gcode.3mf", ".3mf.gcode", ".3mf", ".gcode"];

export function sanitize(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^a-zA-Z0-9-._]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || "anon";
}

export function normalizeExt(name: string): string {
  const lower = name.toLowerCase();
  for (const ext of ALLOWED_EXT) {
    if (lower.endsWith(ext)) {
      return name.slice(0, name.length - ext.length) + ".gcode.3mf";
    }
  }
  return name;
}

export function buildPathname(
  studentName: string,
  targetPrinter: string,
  filename: string,
): string {
  const name = sanitize(studentName);
  const target = sanitize(targetPrinter || "any");
  const file = sanitize(normalizeExt(filename));
  return `incoming/${name}__${target}__${file}`;
}

export interface UploadSubmissionInput {
  studentName: string;
  targetPrinter: string;
  filename: string;
  file: File | Blob;
}

// Server-side put fallback (not used on the main client-upload path).
export async function putSubmission(input: UploadSubmissionInput) {
  const pathname = buildPathname(input.studentName, input.targetPrinter, input.filename);
  return put(pathname, input.file, {
    access: "private",
    addRandomSuffix: false,
    token: process.env.BLOB_READ_WRITE_TOKEN,
  });
}
