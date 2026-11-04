"use client";

import { useCallback, useEffect, useState } from "react";
import { PrinterCard } from "@/components/PrinterCard";
import { printerImage } from "@/lib/printerImages";
import type { PrinterSnapshot } from "@/lib/station";

interface Props {
  initialPrinters: PrinterSnapshot[];
}

const POLL_MS = 20000;

export function PrinterGrid({ initialPrinters }: Props) {
  const [printers, setPrinters] = useState<PrinterSnapshot[]>(initialPrinters);

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
              imageSrc={printerImage(p.model ?? p.name)}
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
