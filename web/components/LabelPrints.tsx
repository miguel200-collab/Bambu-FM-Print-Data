"use client";

import { useEffect, useState } from "react";
import {
  outcomeFromLabel,
  type MyPrint,
  type MyPrintsResponse,
  type Outcome,
} from "@/lib/labeling";

const STORAGE_KEY = "labelNetId";

function formatEnded(iso: string | null): string {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function LabelPrints() {
  const [netid, setNetid] = useState("");
  const [prints, setPrints] = useState<MyPrint[] | null>(null);
  const [windowDays, setWindowDays] = useState(14);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // Per-job UI state: which job is saving, and any per-row message.
  const [saving, setSaving] = useState<string | null>(null);
  const [rowMsg, setRowMsg] = useState<Record<string, { kind: "ok" | "err"; msg: string }>>({});

  // Remember the NetID on this device so students don't retype it.
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) setNetid(saved);
    } catch {
      /* private mode etc. — fine */
    }
  }, []);

  async function loadPrints(e?: React.FormEvent) {
    e?.preventDefault();
    if (!netid.trim()) {
      setError("Please enter your NetID.");
      return;
    }
    setLoading(true);
    setError("");
    setRowMsg({});
    try {
      const res = await fetch(`/api/my-prints?netid=${encodeURIComponent(netid.trim())}`, {
        cache: "no-store",
      });
      const data = (await res.json()) as MyPrintsResponse & { error?: string };
      if (!res.ok) {
        setPrints(null);
        setError(data.error ?? "Could not load your prints.");
        return;
      }
      setPrints(data.prints);
      setWindowDays(data.window_days);
      try {
        localStorage.setItem(STORAGE_KEY, netid.trim());
      } catch {
        /* ignore */
      }
    } catch {
      setPrints(null);
      setError("Could not reach the server. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }

  async function label(job: MyPrint, outcome: Outcome) {
    setSaving(job.job_id);
    setRowMsg((m) => ({ ...m, [job.job_id]: { kind: "ok", msg: "" } }));
    try {
      const res = await fetch("/api/label", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ netid: netid.trim(), job_id: job.job_id, outcome }),
      });
      const data = (await res.json()) as { error?: string; label?: number };
      if (!res.ok) {
        setRowMsg((m) => ({
          ...m,
          [job.job_id]: { kind: "err", msg: data.error ?? "Could not save your label." },
        }));
        return;
      }
      setPrints((cur) =>
        (cur ?? []).map((p) =>
          p.job_id === job.job_id
            ? { ...p, user_label: outcome === "failed" ? 1 : 0, label_source: "web" }
            : p,
        ),
      );
      setRowMsg((m) => ({ ...m, [job.job_id]: { kind: "ok", msg: "Saved. Thank you!" } }));
    } catch {
      setRowMsg((m) => ({
        ...m,
        [job.job_id]: { kind: "err", msg: "Could not reach the server. Try again." },
      }));
    } finally {
      setSaving(null);
    }
  }

  return (
    <div>
      <form className="form" onSubmit={loadPrints}>
        <div className="field">
          <label htmlFor="label-netid">NetID</label>
          <input
            id="label-netid"
            type="text"
            placeholder="ab1234"
            value={netid}
            onChange={(e) => setNetid(e.target.value)}
            autoComplete="off"
          />
          <span className="hint">The same NetID you used when you submitted the file.</span>
        </div>
        <button className="btn" type="submit" disabled={loading}>
          {loading ? "Loading…" : "Show my prints"}
        </button>
        {error && <div className="form-msg err">{error}</div>}
      </form>

      {prints !== null && prints.length === 0 && (
        <div className="center-note" style={{ marginTop: "1.5rem" }}>
          No finished prints from the last {windowDays} days were found for that NetID.
          A print shows up here after it ends, if it was printed from a file you
          submitted through this site.
        </div>
      )}

      {prints !== null && prints.length > 0 && (
        <ul className="print-list">
          {prints.map((p) => {
            const current = outcomeFromLabel(p.user_label);
            const msg = rowMsg[p.job_id];
            return (
              <li key={p.job_id} className="print-row">
                <div className="print-meta">
                  <div className="print-name mono">{p.filename ?? p.subtask_name ?? "print"}</div>
                  <div className="print-sub">
                    {p.printer_name} · ended {formatEnded(p.end_time)}
                  </div>
                  {current && (
                    <div className={`print-current ${current}`}>
                      You labeled this: {current === "failed" ? "failed" : "succeeded"}
                    </div>
                  )}
                </div>
                <div className="print-actions">
                  <button
                    type="button"
                    className="btn ok"
                    disabled={saving === p.job_id}
                    onClick={() => label(p, "succeeded")}
                  >
                    Print succeeded
                  </button>
                  <button
                    type="button"
                    className="btn ghost"
                    disabled={saving === p.job_id}
                    onClick={() => label(p, "failed")}
                  >
                    Print failed
                  </button>
                </div>
                {msg?.msg && <div className={`form-msg ${msg.kind} print-msg`}>{msg.msg}</div>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
