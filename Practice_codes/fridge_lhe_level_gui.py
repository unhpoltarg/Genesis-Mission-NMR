"""GUI for the fridge liquid-helium level probe.

Uses fridge_lhe_level.FridgeLHeLevelReader for the AMI-1700 on
ASRL7::INSTR / Port7-AMI-1700.

Run from the level folder:
    python fridge_lhe_level_gui.py

Run from instrument_readers:
    python level/fridge_lhe_level_gui.py
"""

from __future__ import annotations

import argparse
import csv
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

from fridge_lhe_level import FridgeLHeLevelReader, FridgeLevelRead, decode_escapes, fmt, is_finite


class FridgeLHeLevelGui(tk.Tk):
    BG = "#101820"
    PANEL = "#182430"
    PANEL_2 = "#233244"
    BORDER = "#3b4e62"
    TEXT = "#edf5ff"
    MUTED = "#9fb0c3"
    CYAN = "#38d9ff"
    GREEN = "#66df8c"
    YELLOW = "#ffd166"
    RED = "#ff6673"

    def __init__(self) -> None:
        super().__init__()
        self.title("Fridge LHe Level - AMI 1700")
        self.geometry("1020x700")
        self.minsize(900, 620)
        self.configure(bg=self.BG)

        self.port_var = tk.StringVar(value="ASRL7::INSTR")
        self.baud_var = tk.IntVar(value=115200)
        self.backend_var = tk.StringVar(value="auto")
        self.command_var = tk.StringVar(value=r"MEASure:HE:LEVel?\r")
        self.idn_command_var = tk.StringVar(value=r"*IDN?\r")
        self.scale_var = tk.DoubleVar(value=1.0)
        self.offset_var = tk.DoubleVar(value=0.0)
        self.interval_ms_var = tk.IntVar(value=1000)
        self.empty_level_var = tk.DoubleVar(value=0.05)
        self.low_alarm_var = tk.DoubleVar(value=2.0)
        self.overfill_var = tk.DoubleVar(value=30.9)
        self.simulate_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=True)
        self.idn_each_read_var = tk.BooleanVar(value=False)

        self.level_var = tk.StringVar(value="NaN")
        self.raw_value_var = tk.StringVar(value="NaN")
        self.raw_var = tk.StringVar(value="No response yet")
        self.idn_var = tk.StringVar(value="IDN not read yet")
        self.status_var = tk.StringVar(value="Ready")
        self.backend_status_var = tk.StringVar(value="idle")
        self.alarm_var = tk.StringVar(value="Idle")
        self.csv_path = self._default_csv_path()

        self.samples: deque[float] = deque(maxlen=360)
        self.reader: FridgeLHeLevelReader | None = None
        self.running = False

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(250, self._draw_plot)

    def _build(self) -> None:
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, bg=self.BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(18, 10))
        header.columnconfigure(1, weight=1)
        tk.Label(header, text="Fridge LHe Level", bg=self.BG, fg=self.TEXT, font=("Segoe UI Semibold", 26)).grid(row=0, column=0, sticky="w")
        tk.Label(header, text="AMI 1700 level instrument", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 11)).grid(row=1, column=0, sticky="w")

        actions = tk.Frame(header, bg=self.BG)
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        self._button(actions, "Start", self.start, self.GREEN).grid(row=0, column=0, padx=4)
        self._button(actions, "Stop", self.stop, self.RED).grid(row=0, column=1, padx=4)
        self._button(actions, "Read Once", self.read_once, self.CYAN).grid(row=0, column=2, padx=4)

        main = tk.Frame(self, bg=self.BG)
        main.grid(row=1, column=0, sticky="nsew", padx=(20, 10), pady=(0, 20))
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)
        self._readout_panel(main).grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.plot = tk.Canvas(main, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        self.plot.grid(row=1, column=0, sticky="nsew")

        side = tk.Frame(self, bg=self.BG)
        side.grid(row=1, column=1, sticky="nsew", padx=(10, 20), pady=(0, 20))
        side.columnconfigure(0, weight=1)
        self._connection_panel(side).grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._settings_panel(side).grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._raw_panel(side).grid(row=2, column=0, sticky="ew", pady=(0, 12))

    def _readout_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = self._panel(parent)
        panel.columnconfigure(0, weight=1)
        tk.Label(panel, text="Fridge LHe Level", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 0))
        tk.Label(panel, textvariable=self.level_var, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI Semibold", 56)).grid(row=1, column=0, sticky="w", padx=18)
        tk.Label(panel, text="inch", bg=self.PANEL, fg=self.CYAN, font=("Segoe UI", 18, "bold")).grid(row=1, column=1, sticky="sw", padx=(0, 20), pady=(0, 16))
        self.level_bar = tk.Canvas(panel, width=90, height=178, bg=self.PANEL, highlightthickness=0)
        self.level_bar.grid(row=0, column=2, rowspan=3, padx=18, pady=14)
        tk.Label(panel, textvariable=self.alarm_var, bg=self.PANEL, fg=self.YELLOW, font=("Segoe UI", 11, "bold")).grid(row=2, column=0, sticky="w", padx=18, pady=(0, 4))
        tk.Label(panel, textvariable=self.backend_status_var, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 10)).grid(row=3, column=0, columnspan=3, sticky="w", padx=18, pady=(0, 16))
        return panel

    def _connection_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = self._panel(parent, "Connection")
        self._combo(panel, "Resource", self.port_var, ["ASRL7::INSTR", "COM7", "Port7-AMI-1700"], 1)
        self._field(panel, "Baud", self.baud_var, 2)
        self._combo(panel, "Backend", self.backend_var, ["auto", "serial", "visa"], 3)
        self._check(panel, "Simulation mode", self.simulate_var, 4)
        self._check(panel, "Print terminal debug", self.debug_var, 5)
        panel.columnconfigure(1, weight=1)
        return panel

    def _settings_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = self._panel(parent, "Read And Alarm")
        self._field(panel, "Level command", self.command_var, 1)
        self._field(panel, "IDN command", self.idn_command_var, 2)
        self._field(panel, "Scale", self.scale_var, 3)
        self._field(panel, "Offset", self.offset_var, 4)
        self._field(panel, "Poll ms", self.interval_ms_var, 5)
        self._field(panel, "Empty below in", self.empty_level_var, 6)
        self._field(panel, "Low alarm in", self.low_alarm_var, 7)
        self._field(panel, "Overfill in", self.overfill_var, 8)
        self._check(panel, "Read IDN each poll", self.idn_each_read_var, 9)
        tk.Button(panel, text="CSV log...", command=self._choose_csv, bg=self.PANEL_2, fg=self.TEXT, relief="flat").grid(row=10, column=0, columnspan=2, sticky="ew", padx=14, pady=(10, 14))
        panel.columnconfigure(1, weight=1)
        return panel

    def _raw_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = self._panel(parent, "Last Response")
        tk.Label(panel, text="Instrument ID", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 2))
        tk.Message(panel, textvariable=self.idn_var, bg="#0b111a", fg=self.TEXT, width=340, font=("Consolas", 9), relief="flat").grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))
        tk.Label(panel, text="Parsed raw value", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9, "bold")).grid(row=3, column=0, sticky="w", padx=14, pady=(0, 2))
        tk.Label(panel, textvariable=self.raw_value_var, bg="#0b111a", fg=self.CYAN, font=("Consolas", 11, "bold"), anchor="w").grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 8))
        tk.Message(panel, textvariable=self.raw_var, bg="#0b111a", fg=self.TEXT, width=340, font=("Consolas", 10), relief="flat").grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 14))
        panel.columnconfigure(0, weight=1)
        return panel

    def _panel(self, parent: tk.Widget, title: str | None = None) -> tk.Frame:
        panel = tk.Frame(parent, bg=self.PANEL, highlightthickness=1, highlightbackground=self.BORDER)
        if title:
            tk.Label(panel, text=title, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 8))
        return panel

    def _field(self, parent: tk.Widget, label: str, var: tk.Variable, row: int) -> None:
        tk.Label(parent, text=label, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w", padx=14, pady=6)
        tk.Entry(parent, textvariable=var, bg=self.PANEL_2, fg=self.TEXT, insertbackground=self.TEXT, relief="flat").grid(row=row, column=1, sticky="ew", padx=14, pady=6)

    def _combo(self, parent: tk.Widget, label: str, var: tk.Variable, values: list[str], row: int) -> None:
        tk.Label(parent, text=label, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w", padx=14, pady=6)
        ttk.Combobox(parent, textvariable=var, values=values, state="normal" if label == "Resource" else "readonly").grid(row=row, column=1, sticky="ew", padx=14, pady=6)

    def _check(self, parent: tk.Widget, label: str, var: tk.BooleanVar, row: int) -> None:
        tk.Checkbutton(parent, text=label, variable=var, bg=self.PANEL, fg=self.TEXT, selectcolor=self.PANEL_2, activebackground=self.PANEL, activeforeground=self.TEXT).grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 8))

    def _button(self, parent: tk.Widget, text: str, command, color: str) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=color, fg="#07111f", activebackground=color, activeforeground="#07111f", relief="flat", padx=14, pady=7, font=("Segoe UI", 10, "bold"))

    def start(self) -> None:
        if self.running:
            return
        self.reader = self._new_reader()
        self.running = True
        self.status_var.set("Polling Fridge LHe level...")
        self._poll_once()

    def stop(self) -> None:
        self.running = False
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.status_var.set("Stopped")

    def read_once(self) -> None:
        self.reader = self._new_reader()
        self._read_and_update(include_idn=True)

    def _poll_once(self) -> None:
        if not self.running:
            return
        self._read_and_update(include_idn=bool(self.idn_each_read_var.get()))
        self.after(max(100, int(self.interval_ms_var.get())), self._poll_once)

    def _read_and_update(self, include_idn: bool = False) -> None:
        reader = self.reader or self._new_reader()
        result = reader.read(include_idn=include_idn)
        self.level_var.set(fmt(result.level_in, 2))
        self.raw_value_var.set(fmt(result.raw_value, 4))
        self.raw_var.set(result.raw or "(empty response)")
        if result.idn:
            self.idn_var.set(result.idn)
        backend = getattr(reader, "_active_backend", None) or "simulation"
        self.backend_status_var.set(f"{result.status} via {backend}  |  {time.strftime('%H:%M:%S')}")
        self._update_alarm(result.level_in)
        self.samples.append(result.level_in)
        self._write_csv(result)
        self._draw_plot()

    def _update_alarm(self, level: float) -> None:
        if not is_finite(level):
            self.alarm_var.set("No valid level")
            return
        empty_level = float(self.empty_level_var.get())
        low_alarm = float(self.low_alarm_var.get())
        overfill = float(self.overfill_var.get())
        if level <= empty_level:
            self.alarm_var.set("EMPTY / NO LHe")
        elif level < low_alarm:
            self.alarm_var.set("LOW LEVEL ALARM")
        elif level >= overfill:
            self.alarm_var.set("Overfill region")
        else:
            self.alarm_var.set("Normal")

    def _new_reader(self) -> FridgeLHeLevelReader:
        if self.reader is not None:
            self.reader.close()
        return FridgeLHeLevelReader(
            port=self.port_var.get().strip() or "ASRL7::INSTR",
            baud=int(self.baud_var.get()),
            command=decode_escapes(self.command_var.get()),
            idn_command=decode_escapes(self.idn_command_var.get()),
            scale=float(self.scale_var.get()),
            offset=float(self.offset_var.get()),
            backend=self.backend_var.get(),
            simulate=bool(self.simulate_var.get()),
            timeout_s=max(0.4, int(self.interval_ms_var.get()) / 1000 * 2),
            debug=bool(self.debug_var.get()),
        )

    def _draw_plot(self) -> None:
        if not hasattr(self, "plot"):
            return
        c = self.plot
        w = max(c.winfo_width(), 320)
        h = max(c.winfo_height(), 240)
        c.delete("all")
        x0, y0, x1, y1 = 62, 28, w - 18, h - 42
        c.create_rectangle(x0, y0, x1, y1, fill="#0b111a", outline=self.BORDER)
        for i in range(6):
            x = x0 + (x1 - x0) * i / 5
            c.create_line(x, y0, x, y1, fill="#1f2b3a")
        for i in range(5):
            y = y0 + (y1 - y0) * i / 4
            c.create_line(x0, y, x1, y, fill="#1f2b3a")
        c.create_text(18, 16, text="Level Trend", anchor="nw", fill=self.TEXT, font=("Segoe UI", 14, "bold"))
        c.create_text(16, (y0 + y1) / 2, text="inch", angle=90, fill=self.MUTED, font=("Segoe UI", 10, "bold"))
        c.create_text((x0 + x1) / 2, h - 14, text="Recent samples", fill=self.MUTED, font=("Segoe UI", 10, "bold"))

        values = [v for v in self.samples if is_finite(v)]
        self._draw_level_bar(values[-1] if values else float("nan"))
        if len(values) < 2:
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="Waiting for data", fill=self.MUTED, font=("Segoe UI", 12))
            return

        low_alarm = float(self.low_alarm_var.get())
        overfill = float(self.overfill_var.get())
        lo = min(min(values), low_alarm) - 1
        hi = max(max(values), overfill) + 1
        if hi <= lo:
            hi = lo + 1
        pts: list[float] = []
        all_values = list(self.samples)
        for idx, value in enumerate(all_values):
            if not is_finite(value):
                continue
            x = x0 + (x1 - x0) * idx / max(1, len(all_values) - 1)
            y = y1 - (value - lo) / (hi - lo) * (y1 - y0)
            pts.extend([x, y])
        low_y = y1 - (low_alarm - lo) / (hi - lo) * (y1 - y0)
        over_y = y1 - (overfill - lo) / (hi - lo) * (y1 - y0)
        c.create_line(x0, low_y, x1, low_y, fill=self.RED, dash=(4, 3))
        c.create_line(x0, over_y, x1, over_y, fill=self.YELLOW, dash=(4, 3))
        if len(pts) >= 4:
            c.create_line(*pts, fill=self.CYAN, width=3, smooth=True)
        c.create_text(x0 + 6, y0 + 6, text=fmt(hi), anchor="nw", fill=self.MUTED, font=("Consolas", 9))
        c.create_text(x0 + 6, y1 - 6, text=fmt(lo), anchor="sw", fill=self.MUTED, font=("Consolas", 9))
        c.create_text(x1 - 6, y0 + 6, text=f"last {fmt(values[-1])}", anchor="ne", fill=self.CYAN, font=("Consolas", 10, "bold"))

    def _draw_level_bar(self, level: float) -> None:
        if not hasattr(self, "level_bar"):
            return
        c = self.level_bar
        c.delete("all")
        x0, y0, x1, y1 = 30, 10, 60, 160
        c.create_rectangle(x0, y0, x1, y1, fill="#0b111a", outline=self.BORDER, width=2)
        max_level = max(1.0, float(self.overfill_var.get()) + 5.0)
        if is_finite(level):
            fill_top = y1 - max(0.0, min(level / max_level, 1.0)) * (y1 - y0)
            color = self.RED if level < float(self.low_alarm_var.get()) else self.CYAN
            if level >= float(self.overfill_var.get()):
                color = self.YELLOW
            c.create_rectangle(x0 + 3, fill_top, x1 - 3, y1 - 3, fill=color, outline="")
        low_y = y1 - min(float(self.low_alarm_var.get()) / max_level, 1.0) * (y1 - y0)
        over_y = y1 - min(float(self.overfill_var.get()) / max_level, 1.0) * (y1 - y0)
        c.create_line(x0 - 8, low_y, x1 + 8, low_y, fill=self.RED, width=2)
        c.create_line(x0 - 8, over_y, x1 + 8, over_y, fill=self.YELLOW, width=2)
        c.create_text(45, 172, text="LHe", fill=self.MUTED, font=("Segoe UI", 9, "bold"))

    def _default_csv_path(self) -> Path:
        logs = Path(__file__).resolve().parent.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / f"fridge_lhe_level_{datetime.now().strftime('%Y%m%d')}.csv"

    def _choose_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose Fridge LHe level CSV log",
            initialfile=self.csv_path.name,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.csv_path = Path(path)
            self.status_var.set(f"Logging to {self.csv_path.name}")

    def _write_csv(self, result: FridgeLevelRead) -> None:
        try:
            new_file = not self.csv_path.exists()
            with open(self.csv_path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new_file:
                    writer.writerow(["date", "time", "timestamp", "level_in", "raw_value", "status", "raw", "idn", "port"])
                now = datetime.now()
                writer.writerow([
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M:%S.%f")[:-3],
                    now.isoformat(timespec="milliseconds"),
                    fmt(result.level_in, 4),
                    fmt(result.raw_value, 6),
                    result.status,
                    result.raw,
                    result.idn,
                    self.port_var.get(),
                ])
        except Exception as exc:
            self.status_var.set(f"CSV error: {exc}")

    def _close(self) -> None:
        self.running = False
        if self.reader is not None:
            self.reader.close()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fridge LHe level GUI")
    parser.add_argument("--port", default="ASRL7::INSTR")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--simulate", default="false", choices=["true", "false"])
    parser.add_argument("--debug", default="true", choices=["true", "false"])
    args = parser.parse_args()

    app = FridgeLHeLevelGui()
    app.port_var.set(args.port)
    app.baud_var.set(args.baud)
    app.simulate_var.set(args.simulate.lower() == "true")
    app.debug_var.set(args.debug.lower() == "true")
    app.mainloop()


if __name__ == "__main__":
    main()
