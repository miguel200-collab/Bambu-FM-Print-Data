"use client";

import { useCallback, useEffect, useState } from "react";
import { PrinterCard } from "@/components/PrinterCard";
import type { PrinterSnapshot } from "@/lib/station";

interface Props {
  initialPrinters: PrinterSnapshot[];
  stationApiUrl: string;
}

const POLL_MS = 20000;
const CAMERA_POLL_MS = 2000;

function cameraSrc(stationApiUrl: string, serial: string, refreshKey: number): string {
  const base = `${stationApiUrl}/api/printer/${encodeURIComponent(serial)}/camera`;
  return refreshKey > 0 ? `${base}?t=${refreshKey}` : base;
}

export function PrinterGrid({ initialPrinters, stationApiUrl }: Props) {
  const [printers, setPrinters] = useState<PrinterSnapshot[]>(initialPrinters);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/printers", { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as PrinterSnapshot[];
      setPrinters(data);
    } catch {
      /* leave last known state in place */
    }
  }, []);

  // Status poll: every 20s + on focus/visibility.
  useEffect(() => {
    const id = window.setInterval(refresh, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [refresh]);

  // Camera refresh: every 2s, cache-busted via refreshKey. Decoupled from the
  // status poll so the feed feels live without re-fetching the JSON status.
  // Pauses when the tab is hidden to avoid loading frames no one is looking at.
  useEffect(() => {
    let id: number | undefined;
    const tick = () => {
      if (!document.hidden) setRefreshKey((k) => k + 1);
    };
    id = window.setInterval(tick, CAMERA_POLL_MS);
    return () => {
      if (id) window.clearInterval(id);
    };
  }, []);

  const online = printers.length > 0;
  const connectedCount = printers.filter((p) => p.connected).length;

  return (
    <section id="printers" className="section">
      <div className="section-head">
        <div>
          <span className="section-index mono">[ 01 ]</span>
          <span className="section-title">Printers</span>
        </div>
        <span className="section-meta">
          {online ? `${connectedCount} live` : "offline"}
        </span>
      </div>

      {online ? (
        <div className="grid">
          {printers.map((p) => (
            <PrinterCard
              key={p.serial}
              printer={p}
              cameraSrc={
                stationApiUrl ? cameraSrc(stationApiUrl, p.serial, refreshKey) : null
              }
            />
          ))}
        </div>
      ) : (
        <div className="center-note">
          The station is not reachable right now. Printer status will appear here
          once the dedicated laptop is online.
        </div>
      )}
    </section>
  );
}
