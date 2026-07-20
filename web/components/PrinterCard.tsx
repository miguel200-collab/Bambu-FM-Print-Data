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
  cameraSrc: string | null;
}

function fmtTemp(t: number | null): string {
  return t == null ? "—" : `${t.toFixed(0)}°C`;
}

export function PrinterCard({ printer, cameraSrc }: Props) {
  const state = (printer.state || (printer.connected ? "IDLE" : "OFFLINE")).toUpperCase();

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <div className="card-name">{printer.name}</div>
          <div className="card-serial mono">{printer.serial}</div>
        </div>
        <StatusBadge state={state} connected={printer.connected} />
      </div>

      {cameraSrc ? (
        <img className="camera" src={cameraSrc} alt={`${printer.name} camera`} />
      ) : (
        <div className="camera-fallback">No camera available</div>
      )}

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
