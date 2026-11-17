import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PrinterCard } from "./PrinterCard";

const SERIAL = "01S00C123456789";

describe("PrinterCard", () => {
  it("does not render the printer serial", () => {
    const html = renderToStaticMarkup(
      createElement(PrinterCard, {
        imageSrc: "/printers/x1c.svg",
        printer: {
          name: "The Allee",
          serial: SERIAL,
          connected: true,
          state: "IDLE",
          subtask_name: "bracket.gcode.3mf",
          nozzle_temper: 218,
          bed_temper: 60,
          filament_type: "PLA",
        },
      }),
    );

    expect(html).toContain("The Allee");
    expect(html).not.toContain(SERIAL);
  });
});
