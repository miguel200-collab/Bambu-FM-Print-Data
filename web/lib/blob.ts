// blob.ts — pure helpers for student submission pathnames and file validation.
//
// The pathname encodes everything the station's file_watcher needs to route the
// file, so no separate metadata store is required:
//   incoming/<safeName>__<targetSerial>__<originalFilename>
// where targetSerial is "any" if the student didn't pick a specific printer.
//
// With client-side uploads (handleUpload/upload from @vercel/blob/client), the
// browser builds the pathname and uploads the file directly to Vercel Blob; the
// server only validates the pathname in onBeforeGenerateToken.
//
// This module deliberately imports nothing from the Vercel SDK: it is shared by
// the browser form and the server route, and is unit-tested in isolation.

// Only a SLICED export is accepted. A plain ".gcode" or a ".3mf" model is NOT a
// valid Bambu print package: renaming one to ".gcode.3mf" doesn't change its
// bytes, and Bambu Farm Manager rejects it with a generic "file error".
export const REQUIRED_EXT = ".gcode.3mf";
export const ALLOWED_EXT: readonly string[] = [REQUIRED_EXT];

export const EXPORT_HINT =
  "Export from Bambu Studio as .gcode.3mf (File > Export > Export plate sliced file).";

// A real .gcode.3mf is a ZIP package: it starts with "PK\x03\x04".
export const ZIP_SIGNATURE: readonly number[] = [0x50, 0x4b, 0x03, 0x04];

export function sanitize(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^a-zA-Z0-9-._]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || "anon";
}

// True for "<something>.gcode.3mf" (case-insensitive) with a non-empty stem.
export function isAllowedFilename(name: string): boolean {
  const lower = name.toLowerCase();
  return ALLOWED_EXT.some((ext) => lower.endsWith(ext) && lower.length > ext.length);
}

// Canonicalise the extension's case ("Part.GCODE.3MF" -> "Part.gcode.3mf").
// Names without the required extension are returned unchanged — they are
// rejected by isAllowedFilename, never silently renamed.
export function normalizeExt(name: string): string {
  if (name.toLowerCase().endsWith(REQUIRED_EXT)) {
    return name.slice(0, name.length - REQUIRED_EXT.length) + REQUIRED_EXT;
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

export interface ParsedPathname {
  name: string;
  target: string;
  file: string;
}

// Inverse of buildPathname, just enough to validate server-side. Returns null
// when the pathname doesn't have at least "<name>__<file>".
export function parsePathname(pathname: string): ParsedPathname | null {
  const body = pathname.startsWith("incoming/") ? pathname.slice("incoming/".length) : pathname;
  const parts = body.split("__");
  if (parts.length < 2) return null;
  return {
    name: parts[0],
    target: parts.length >= 3 ? parts[1] : "any",
    file: parts[parts.length - 1],
  };
}

export function hasZipSignature(bytes: ArrayLike<number>): boolean {
  if (bytes.length < ZIP_SIGNATURE.length) return false;
  return ZIP_SIGNATURE.every((b, i) => bytes[i] === b);
}
