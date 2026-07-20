"use client";

import { useState } from "react";
import type { PrinterSnapshot } from "@/lib/station";

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
    setStatus({ kind: null, msg: "" });
    try {
      const fd = new FormData();
      fd.append("studentName", name.trim());
      fd.append("targetPrinter", target);
      fd.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || "Upload failed");
      }
      setStatus({
        kind: "ok",
        msg: `Submitted! The station will upload "${file.name}" to the printer shortly.`,
      });
      setFile(null);
    } catch (err) {
      setStatus({ kind: "err", msg: err instanceof Error ? err.message : "Upload failed" });
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

      {status.kind && (
        <div className={`form-msg ${status.kind}`}>{status.msg}</div>
      )}
    </form>
  );
}
