"use client";

import { useState } from "react";
import { upload } from "@vercel/blob/client";
import type { PrinterSnapshot } from "@/lib/station";
import {
  buildPathname,
  EXPORT_HINT,
  hasZipSignature,
  isAllowedFilename,
} from "@/lib/blob";

interface Props {
  printers: PrinterSnapshot[];
}

export function UploadForm({ printers }: Props) {
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState("any");
  const [status, setStatus] = useState<{ kind: "ok" | "err" | null; msg: string }>({
    kind: null,
    msg: "",
  });
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setStatus({ kind: "err", msg: "Please enter your NetID." });
      return;
    }
    if (!file) {
      setStatus({ kind: "err", msg: "Please choose a .gcode.3mf file." });
      return;
    }
    if (!isAllowedFilename(file.name)) {
      setStatus({
        kind: "err",
        msg: `"${file.name}" is not a .gcode.3mf. A plain .3mf model or .gcode file will not print. ${EXPORT_HINT}`,
      });
      return;
    }
    // A real .gcode.3mf is a ZIP package. Catches a file that was merely
    // renamed to end in .gcode.3mf before it wastes an upload.
    const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
    if (!hasZipSignature(head)) {
      setStatus({
        kind: "err",
        msg: `"${file.name}" does not look like a sliced Bambu Studio export (it is not a 3MF package). ${EXPORT_HINT}`,
      });
      return;
    }
    setBusy(true);
    setProgress(0);
    setStatus({ kind: null, msg: "" });
    try {
      const pathname = buildPathname(name.trim(), target, file.name);
      const blob = await upload(pathname, file, {
        access: "private",
        handleUploadUrl: "/api/upload",
        multipart: true,
        clientPayload: JSON.stringify({ studentName: name.trim(), targetPrinter: target }),
        onUploadProgress: (evt) => setProgress(Math.round(evt.percentage)),
      });
      setStatus({
        kind: "ok",
        msg: `Archived "${file.name}" to the lab inbox. Open Bambu Farm Manager on the laptop and upload it to a printer from there.`,
      });
      setFile(null);
      setProgress(null);
      void blob;
    } catch (err) {
      setStatus({ kind: "err", msg: err instanceof Error ? err.message : "Upload failed" });
      setProgress(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={onSubmit}>
      <div className="field">
        <label htmlFor="name">NetID</label>
        <input
          id="name"
          type="text"
          placeholder="ab1234"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <span className="hint">Your Cornell NetID — used as your inbox folder name.</span>
      </div>

      <div className="field">
        <label htmlFor="target">Target printer</label>
        <select id="target" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="any">Any free printer</option>
          {printers.map((p) => {
            const state = (p.state || (p.connected ? "IDLE" : "OFFLINE")).toUpperCase();
            const label = p.connected ? `${p.name} — ${state}` : `${p.name} — OFFLINE`;
            return (
              <option key={p.serial} value={p.serial}>
                {label}
              </option>
            );
          })}
        </select>
      </div>

      <div className="field full">
        <label htmlFor="file">Sliced file (.gcode.3mf)</label>
        <input
          id="file"
          type="file"
          accept=".3mf"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <span className="hint">{EXPORT_HINT}</span>
      </div>

      <button className="btn" type="submit" disabled={busy}>
        {busy ? "Submitting…" : "Submit print"}
      </button>

      {progress !== null && (
        <div className="form-msg" aria-live="polite">
          Uploading… {progress}%
          <progress value={progress} max={100} style={{ width: "100%", marginTop: 6 }} />
        </div>
      )}

      {status.kind && (
        <div className={`form-msg ${status.kind}`}>
          {status.msg}
          {status.kind === "ok" && (
            <>
              {" "}
              When your print finishes,{" "}
              <a href="/label" style={{ textDecoration: "underline" }}>
                tell us how it turned out
              </a>
              .
            </>
          )}
        </div>
      )}
    </form>
  );
}
