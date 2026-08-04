import { PrinterCard } from "@/components/PrinterCard";
import { UploadForm } from "@/components/UploadForm";
import { ThemeToggle } from "@/components/ThemeToggle";
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
  const online = printers.length > 0;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" />
            MAKERLAB <span className="brand-dim">// CORNELL TECH</span>
          </div>
          <nav className="nav">
            <a href="#printers" className="active">Printers</a>
            <a href="#submit">Submit</a>
            <a href="#about">About</a>
          </nav>
          <div className="topbar-right">
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="page">
        <div className="subhead">
          <span className="live">
            <span className={`dot${online ? "" : " off"}`} />
            {online
              ? `${connectedCount}/${printers.length} printers connected`
              : "Station offline"}
          </span>
          <span className="sep">//</span>
          <span>Bambu Farm Manager</span>
          <span className="sep">//</span>
          <span>Roosevelt Island, NYC</span>
        </div>

        <section className="hero">
          <h1 className="hero-title">
            <span className="plus">+</span>PRINTERS <span className="dim">// FARM</span>
          </h1>
          <p className="hero-sub">
            Live status and camera feeds from the MakerLAB Bambu fleet. Submit
            a sliced print from home and pick it up from the lab&apos;s upload
            inbox on the dedicated laptop.
          </p>
        </section>

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
                  cameraSrc={cameraUrl(p.serial)}
                />
              ))}
            </div>
          ) : (
            <div className="center-note">
              The station is not reachable right now. Printer status will appear
              here once the dedicated laptop is online.
            </div>
          )}
        </section>

        <section id="submit" className="section form-section">
          <div className="section-head">
            <div>
              <span className="section-index mono">[ 02 ]</span>
              <span className="section-title">Submit a print</span>
            </div>
            <span className="section-meta">.gcode.3mf only</span>
          </div>
          <p>
            Export the sliced file from Bambu Studio as{" "}
            <span className="mono">.gcode.3mf</span> (the bundled 3MF format),
            not plain <span className="mono">.gcode</span> or a{" "}
            <span className="mono">.3mf</span> model. The dedicated laptop will
            download it and archive it to the lab inbox — open Bambu Farm
            Manager on the laptop and upload it to a printer from there.
          </p>
          <UploadForm printers={printers} />
        </section>

        <section id="about" className="section">
          <div className="section-head">
            <div>
              <span className="section-index mono">[ 03 ]</span>
              <span className="section-title">About</span>
            </div>
          </div>
          <p className="hero-sub" style={{ maxWidth: 640 }}>
            This dashboard is a custom monitor for the Cornell Tech MakerLAB
            Bambu fleet. It is separate from the official Bambu Farm Manager
            app that runs on the same laptop. Printer telemetry is read over
            MQTT; uploads flow through Vercel Blob to the lab&apos;s inbox.
          </p>
        </section>

        <footer className="footer">
          <span>Cornell Tech MakerLAB</span>
          <span>Bambu Farm Dashboard</span>
        </footer>
      </main>

      <a className="fab" href="#submit" aria-label="Submit a print" title="Submit a print">
        <span className="mono">&gt;_</span>
      </a>
    </>
  );
}
