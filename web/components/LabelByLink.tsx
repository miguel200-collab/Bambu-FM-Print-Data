"use client";

import { useState } from "react";
import type { Outcome } from "@/lib/labeling";

interface Props {
  token: string;
  filename?: string;
  printer?: string;
}

// Shown when a student arrives from a (future) label email: the signed token
// already identifies the print, so there is no NetID box. The student still has
// to press a button — merely opening the link never records anything.
export function LabelByLink({ token, filename, printer }: Props) {
  const [saving, setSaving] = useState(false);
  const [done, setDone] = useState<Outcome | null>(null);
  const [error, setError] = useState("");

  async function submit(outcome: Outcome) {
    setSaving(true);
    setError("");
    try {
      const res = await fetch("/api/label", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token, outcome }),
      });
      const data = (await res.json()) as { error?: string };
      if (!res.ok) {
        setError(data.error ?? "Could not save your label.");
        return;
      }
      setDone(outcome);
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setSaving(false);
    }
  }

  if (done) {
    return (
      <div className="form-msg ok">
        Saved — you labeled this print as {done === "failed" ? "failed" : "succeeded"}. Thank
        you! <a href="/label" style={{ textDecoration: "underline" }}>Label another print</a>
      </div>
    );
  }

  return (
    <div>
      <ul className="print-list">
        <li className="print-row">
          <div className="print-meta">
            <div className="print-name mono">{filename ?? "your print"}</div>
            {printer && <div className="print-sub">{printer}</div>}
          </div>
          <div className="print-actions">
            <button type="button" className="btn ok" disabled={saving} onClick={() => submit("succeeded")}>
              Print succeeded
            </button>
            <button type="button" className="btn ghost" disabled={saving} onClick={() => submit("failed")}>
              Print failed
            </button>
          </div>
        </li>
      </ul>
      {error && <div className="form-msg err" style={{ marginTop: "0.75rem" }}>{error}</div>}
    </div>
  );
}
