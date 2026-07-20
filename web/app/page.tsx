import { PrinterCard } from "@/components/PrinterCard";
import { UploadForm } from "@/components/UploadForm";
import { fetchPrinters, cameraUrl, type PrinterSnapshot } from "@/lib/station";

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function loadPrinters(): Promise<PrinterSnapshot[]> {
  try {
    return await fetchPrinters();
  } catch {
    return [];
  }
}

export default async function Page() {
  const printers = await loadPrinters();
  const connectedCount = printers.filter((p) => p.connected).length;

  return (
    <main className="page">
      <header className="header">
        <div className="brand">
          <div className="brand-mark" />
          <div>
            <div className="brand-title">Cornell Tech MakerLAB</div>
            <div className="brand-sub">Bambu Farm Manager</div>
          </div>
        </div>
        <div className="header-meta">
          <span className="dot" />
          {printers.length
            ? `${connectedCount}/${printers.length} printers connected`
            : "Station offline"}
        </div>
      </header>

      {printers.length ? (
        <section className="grid">
          {printers.map((p) => (
            <PrinterCard
              key={p.serial}
              printer={p}
              cameraSrc={cameraUrl(p.serial)}
            />
          ))}
        </section>
      ) : (
        <div className="center-note">
          The station is not reachable right now. Printer status will appear here
          once the dedicated laptop is online.
        </div>
      )}

      <section className="form-section">
        <h2>Submit a print</h2>
        <p>
          Upload your sliced <span className="mono">.gcode.3mf</span> file. The
          dedicated laptop will download it and push it to the printer&apos;s
          Files tab — start it from the printer whenever you&apos;re ready.
        </p>
        <UploadForm printers={printers} />
      </section>
    </main>
  );
}
