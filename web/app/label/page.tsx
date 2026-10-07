import type { Metadata } from "next";
import { ThemeToggle } from "@/components/ThemeToggle";
import { LabelByLink } from "@/components/LabelByLink";
import { LabelPrints } from "@/components/LabelPrints";
import { FAILURE_DEFINITION } from "@/lib/labeling";
import { resolveLabelLink } from "@/lib/labelLinks";

// Depends on the ?t= query string (future email links).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Label my print — MakerLAB // Cornell Tech",
  description: "Tell the MakerLAB how your print turned out.",
};

export default function LabelPage({
  searchParams,
}: {
  searchParams: { t?: string | string[] };
}) {
  const t = typeof searchParams.t === "string" ? searchParams.t : undefined;
  const { linked, problem } = resolveLabelLink(t);

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" />
            MAKERLAB <span className="brand-dim">// CORNELL TECH</span>
          </div>
          <nav className="nav">
            <a href="/">Printers</a>
            <a href="/#submit">Submit</a>
            <a href="/label" className="active">Label</a>
          </nav>
          <div className="topbar-right">
            <ThemeToggle />
          </div>
        </div>
      </header>

      <main className="page">
        <section className="hero">
          <h1 className="hero-title">
            <span className="plus">+</span>LABEL <span className="dim">// MY PRINT</span>
          </h1>
          <p className="hero-sub">
            Once your print is done, tell us how it turned out. Your answer is
            the ground truth for the lab&apos;s print-failure study — it takes
            ten seconds.
          </p>
        </section>

        <section className="section form-section">
          <div className="section-head">
            <div>
              <span className="section-index mono">[ 01 ]</span>
              <span className="section-title">How did it turn out?</span>
            </div>
            <span className="section-meta">last 14 days</span>
          </div>
          <p>{FAILURE_DEFINITION}</p>
          {problem && <div className="form-msg err" style={{ marginBottom: "1rem" }}>{problem}</div>}
          {linked ? (
            <LabelByLink token={linked.token} filename={linked.filename} printer={linked.printer} />
          ) : (
            <LabelPrints />
          )}
        </section>

        <footer className="footer">
          <span>Cornell Tech MakerLAB</span>
          <span>Bambu Farm Dashboard</span>
        </footer>
      </main>
    </>
  );
}
