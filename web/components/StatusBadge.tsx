interface Props {
  state: string;
  connected: boolean;
}

export function StatusBadge({ state, connected }: Props) {
  let cls = "badge ";
  let label = state;

  if (!connected && state !== "RUNNING" && state !== "PREPARE") {
    cls += "badge-offline";
    label = "OFFLINE";
  } else if (state === "RUNNING") {
    cls += "badge-running";
  } else if (state === "PREPARE") {
    cls += "badge-prepare";
  } else if (state === "FAILED") {
    cls += "badge-failed";
  } else {
    cls += "badge-idle";
    label = label || "IDLE";
  }

  return <span className={cls}>{label}</span>;
}
