// blob.ts — helpers for writing student submissions to Vercel Blob.
//
// The pathname encodes everything the station's file_watcher needs to route the
// file, so no separate metadata store is required:
//   incoming/<safeName>__<targetSerial>__<originalFilename>
// where targetSerial is "any" if the student didn't pick a specific printer.

import { put } from "@vercel/blob";

function sanitize(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^a-zA-Z0-9-._]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || "anon";
}

export interface UploadSubmissionInput {
  studentName: string;
  targetPrinter: string; // printer serial, or "any"
  filename: string;
  file: File | Blob;
}

export async function putSubmission(input: UploadSubmissionInput) {
  const name = sanitize(input.studentName);
  const target = sanitize(input.targetPrinter || "any");
  const filename = sanitize(input.filename);
  const pathname = `incoming/${name}__${target}__${filename}`;

  return put(pathname, input.file, {
    access: "private",
    addRandomSuffix: false,
    token: process.env.BLOB_READ_WRITE_TOKEN,
  });
}
