"""
labeler_gui.py — Tkinter popup that asks staff to label a completed print job.

Rules:
- This module contains NO business logic and NO direct database calls.
- The caller (main.py) supplies a callback; this module just invokes it.
- All Tkinter operations must happen on the main thread.
"""

import tkinter as tk
from tkinter import font as tkfont
from datetime import datetime, timezone
from typing import Callable


class LabelPopup(tk.Toplevel):
    """
    Modal-style topmost window that appears when a print job ends.

    Parameters
    ----------
    parent       : The root Tk window (must be passed so Toplevel attaches correctly).
    printer_name : Human-readable printer label, e.g. "Printer 2 — Bambu X1C".
    subtask_name : Filename of the print job, or None if unknown.
    on_label     : Callback invoked with (user_label: int, label_time: str).
                   user_label = 0 → succeeded, 1 → failed.
    on_close     : Optional callback invoked (no args) after the popup is
                   dismissed. Used by the caller to show the next queued popup.
    """

    _WINDOW_WIDTH = 460
    _WINDOW_HEIGHT = 240

    # ---- Colour palette (blackish / whiteish / green) ----
    _BG_DARK   = "#1f1f1f"   # blackish window background
    _FG_LIGHT  = "#f5f5f5"   # whiteish primary text
    _FG_MUTED  = "#9aa0a6"   # muted gray for the filename
    _GREEN     = "#2ecc71"   # success green
    _GREEN_HOV = "#27ae60"
    _GREEN_TXT = "#0b1f14"   # near-black text on green
    _WHITE_BTN = "#e8e8e8"   # whiteish "failed" button
    _WHITE_HOV = "#cfcfcf"
    _DARK_TXT  = "#1a1a1a"

    def __init__(
        self,
        parent: tk.Tk,
        printer_name: str,
        subtask_name: str | None,
        on_label: Callable[[int, str], None],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_label = on_label
        self._on_close = on_close

        self.title("Print Job Complete")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()  # block interaction with the parent window

        self._build_ui(printer_name, subtask_name)
        self._center()

        # Prevent accidental close via the X button from leaving the job unlabeled.
        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, printer_name: str, subtask_name: str | None) -> None:
        PAD = 18
        self.configure(bg=self._BG_DARK)

        heading_font = tkfont.Font(family="Helvetica", size=14, weight="bold")
        body_font    = tkfont.Font(family="Helvetica", size=11)
        btn_font     = tkfont.Font(family="Helvetica", size=12, weight="bold")

        # ---- Question label ----
        question = tk.Label(
            self,
            text="Did this print fail?",
            font=heading_font,
            bg=self._BG_DARK,
            fg=self._FG_LIGHT,
            pady=PAD,
        )
        question.pack(fill="x", padx=PAD)

        # ---- Printer / file info ----
        file_text = subtask_name if subtask_name else "(filename unknown)"
        info = tk.Label(
            self,
            text=f"{printer_name}\n{file_text}",
            font=body_font,
            bg=self._BG_DARK,
            fg=self._FG_MUTED,
            justify="center",
        )
        info.pack(fill="x", padx=PAD)

        # ---- Button row ----
        btn_frame = tk.Frame(self, bg=self._BG_DARK, pady=PAD)
        btn_frame.pack(fill="x", padx=PAD)

        # NO = success (green). highlightbackground is a macOS fallback since the
        # native Aqua theme ignores bg on tk.Button.
        btn_no = tk.Button(
            btn_frame,
            text="NO — Print Succeeded",
            font=btn_font,
            bg=self._GREEN,
            fg=self._GREEN_TXT,
            activebackground=self._GREEN_HOV,
            activeforeground=self._GREEN_TXT,
            highlightbackground=self._GREEN,
            relief="flat",
            padx=16,
            pady=12,
            cursor="hand2",
            command=lambda: self._submit(0),
        )
        btn_no.pack(side="left", expand=True, fill="x", padx=(0, 8))

        # YES = failure (whiteish, dark text) — keeps the palette to black/white/green.
        btn_yes = tk.Button(
            btn_frame,
            text="YES — Print Failed",
            font=btn_font,
            bg=self._WHITE_BTN,
            fg=self._DARK_TXT,
            activebackground=self._WHITE_HOV,
            activeforeground=self._DARK_TXT,
            highlightbackground=self._WHITE_BTN,
            relief="flat",
            padx=16,
            pady=12,
            cursor="hand2",
            command=lambda: self._submit(1),
        )
        btn_yes.pack(side="left", expand=True, fill="x", padx=(8, 0))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _center(self) -> None:
        """Position the window in the centre of the screen."""
        self.update_idletasks()
        w = self._WINDOW_WIDTH
        h = self._WINDOW_HEIGHT
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _submit(self, label: int) -> None:
        label_time = datetime.now(timezone.utc).isoformat()
        self._on_label(label, label_time)
        self.destroy()
        if self._on_close is not None:
            self._on_close()

    def _on_close_attempt(self) -> None:
        """Ignore window-close (X button) — staff must click a button to label."""
        pass
