"use client";

import { useState } from "react";
import { upload } from "@vercel/blob/client";
import type { PrinterSnapshot } from "@/lib/station";
import { buildPathname } from "@/lib/blob";

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
      setStatus({ kind: "err", msg: "Please enter your name." });
      return;
    }
    if (!file) {
      setStatus({ kind: "err", msg: "Please choose a .gcode.3mf file." });
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
        msg: `Submitted! The station will upload "${file.name}" to the printer shortly.`,
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
        <label htmlFor="name">Your name</label>
        <input
          id="name"
          type="text"
          placeholder="Jane Doe"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="target">Target printer</label>
        <select id="target" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="any">Any free printer</option>
          {printers.map((p) => (
            <option key={p.serial} value={p.serial}>
              {p.name}
            </option>
          ))}
        </select>
      </div>

      <div className="field full">
        <label htmlFor="file">Sliced file (.gcode.3mf)</label>
        <input
          id="file"
          type="file"
          accept=".3mf,.gcode,.gcode.3mf,.3mf.gcode"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
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
        <div className={`form-msg ${status.kind}`}>{status.msg}</div>
      )}
    </form>
  );
}
