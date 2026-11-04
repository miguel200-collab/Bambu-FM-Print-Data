// printerImages.ts — static printer illustrations.
//
// The on-printer cameras (port 1984 JPEG snapshots) have been unreliable, so the
// dashboard shows a static illustration instead of a live feed. The image is
// chosen from the printer's model; printers of the same model share artwork.
//
// Model detection is deliberately done from the human-readable name (e.g.
// "Printer 1 — Bambu X1C") or an explicit `model` field if the station starts
// sending one. Printer serials are NOT used here: they are fleet-specific and
// must not be committed to the repo.

export type PrinterModel = "x1c" | "p1s" | "a1" | "generic";

export const IMAGE_BY_MODEL: Record<PrinterModel, string> = {
  x1c: "/printers/x1c.svg",
  p1s: "/printers/p1s.svg",
  a1: "/printers/a1.svg",
  generic: "/printers/generic.svg",
};

// Order matters: most specific patterns first.
const MODEL_PATTERNS: Array<{ pattern: RegExp; model: PrinterModel }> = [
  { pattern: /\bx1\s*(?:c|e|carbon)\b|\bx1\b/i, model: "x1c" },
  { pattern: /\bp1\s*(?:s|p)\b|\bp1\b/i, model: "p1s" },
  { pattern: /\ba1\s*mini\b|\ba1\b/i, model: "a1" },
];

/** Normalise a printer name/model string to a known illustration key. */
export function printerModel(label: string | null | undefined): PrinterModel {
  if (!label) return "generic";
  for (const { pattern, model } of MODEL_PATTERNS) {
    if (pattern.test(label)) return model;
  }
  return "generic";
}

/** Public path of the illustration to show for a printer name/model. */
export function printerImage(label: string | null | undefined): string {
  return IMAGE_BY_MODEL[printerModel(label)];
}
