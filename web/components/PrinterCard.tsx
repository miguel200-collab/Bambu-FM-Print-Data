import { StatusBadge } from "@/components/StatusBadge";

interface Props {
  printer: {
    name: string;
    serial: string;
    connected: boolean;
    state: string | null;
    subtask_name: string | null;
    nozzle_temper: number | null;
    bed_temper: number | null;
    filament_type: string | null;
  };
  /** Static illustration for this printer's model (see lib/printerImages.ts). */
  imageSrc: string;
}

function fmtTemp(t: number | null): string {
  return t == null ? "—" : `${t.toFixed(0)}°C`;
}

export function PrinterCard({ printer, imageSrc }: Props) {
  const state = (printer.state || (printer.connected ? "IDLE" : "OFFLINE")).toUpperCase();

  return (
    <div className="card">
      <div className="card-head">
        <div className="card-name">{printer.name}</div>
        <StatusBadge state={state} connected={printer.connected} />
      </div>

      <img className="printer-image" src={imageSrc} alt="" aria-hidden="true" />

      <div className="metrics">
        <div className="metric">
          <span className="metric-label">Nozzle</span>
          <span className="metric-value mono">{fmtTemp(printer.nozzle_temper)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Bed</span>
          <span className="metric-value mono">{fmtTemp(printer.bed_temper)}</span>
        </div>
        <div className="metric">
          <span className="metric-label">Filament</span>
          <span className="metric-value">{printer.filament_type ?? "—"}</span>
        </div>
        <div className="metric">
          <span className="metric-label">File</span>
          <span className="metric-value mono" title={printer.subtask_name ?? ""}>
            {printer.subtask_name ?? "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
