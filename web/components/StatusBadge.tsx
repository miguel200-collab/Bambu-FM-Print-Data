interface Props {
  state: string;
  connected: boolean;
}

export function StatusBadge({ state, connected }: Props) {
  let cls = "tag ";
  let label = state;

  if (!connected && state !== "RUNNING" && state !== "PREPARE") {
    cls += "tag-offline";
    label = "OFFLINE";
  } else if (state === "RUNNING") {
    cls += "tag-running";
    label = "RUNNING";
  } else if (state === "PREPARE") {
    cls += "tag-prepare";
    label = "PREPARE";
  } else if (state === "FAILED") {
    cls += "tag-failed";
    label = "FAILED";
  } else {
    cls += "tag-idle";
    label = label || "IDLE";
  }

  return <span className={cls}>{label}</span>;
}
