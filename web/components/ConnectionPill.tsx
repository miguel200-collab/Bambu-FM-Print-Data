"use client";

import { useCallback, useEffect, useState } from "react";
import type { PrinterSnapshot } from "@/lib/station";

interface Props {
  initialPrinters: PrinterSnapshot[];
}

const POLL_MS = 20000;

export function ConnectionPill({ initialPrinters }: Props) {
  const [printers, setPrinters] = useState<PrinterSnapshot[]>(initialPrinters);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/printers", { cache: "no-store" });
      if (!res.ok) return;
      setPrinters((await res.json()) as PrinterSnapshot[]);
    } catch {
      /* keep last known */
    }
  }, []);

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
    <span className="live">
      <span className={`dot${online ? "" : " off"}`} />
      {online
        ? `${connectedCount}/${printers.length} printers connected`
        : "Station offline"}
    </span>
  );
}
