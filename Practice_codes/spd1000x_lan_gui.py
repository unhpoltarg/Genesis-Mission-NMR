#!/usr/bin/env python3
"""
spd1000x_lan_gui.py - LAN/Ethernet GUI for a Siglent SPD1000X DC power supply.

Read the supply IP from its front panel (System > LAN), type it here, Connect.
Default transport is VXI-11 (VISA) which puts the supply in remote mode so
Set V / Set I take effect - just like USB. Raw socket (port 5025) is a fallback.

Install:  pip install matplotlib pyvisa pyvisa-py
Run:      python spd1000x_lan_gui.py   [--ip 192.168.1.50]
"""

import argparse, csv, os, platform, queue, socket, subprocess, threading, time
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

SAMPLE_INTERVAL = 1.0
MAX_POINTS = 300
SOCKET_PORT = 5025


class SocketSPD:
    """Raw TCP socket transport (port 5025). Needs only standard Python."""
    def __init__(self, ip, port=SOCKET_PORT, timeout=5.0):
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.sock.settimeout(timeout)
        try:
            self.sock.sendall(b"SYSTem:REMote\n"); time.sleep(0.1)
        except Exception:
            pass
    def write(self, cmd):
        self.sock.sendall((cmd + "\n").encode("ascii"))
        time.sleep(0.1)
    def query(self, cmd):
        self.write(cmd)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("ascii", errors="replace").strip()
    def close(self):
        try: self.sock.close()
        except Exception: pass


class VisaSPD:
    """VXI-11 transport via PyVISA ('TCPIP0::<ip>::INSTR')."""
    def __init__(self, ip, timeout=5.0):
        import pyvisa
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(f"TCPIP0::{ip}::INSTR")
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.inst.timeout = int(timeout * 1000)
        try: self.inst.write("SYSTem:REMote")
        except Exception: pass
    def write(self, cmd): self.inst.write(cmd)
    def query(self, cmd): return self.inst.query(cmd).strip()
    def close(self):
        try: self.inst.close()
        except Exception: pass


class SPD:
    """SCPI wrapper over either transport."""
    def __init__(self, ip, transport="visa"):
        self.io = VisaSPD(ip) if transport == "visa" else SocketSPD(ip)
    def idn(self): return self.io.query("*IDN?")
    def set_voltage(self, v): self.io.write(f"CH1:VOLTage {float(v):.3f}")
    def set_current(self, a): self.io.write(f"CH1:CURRent {float(a):.3f}")
    def output(self, on): self.io.write(f"OUTPut CH1,{'ON' if on else 'OFF'}")
    def measure_voltage(self): return float(self.io.query("MEASure:VOLTage? CH1"))
    def measure_current(self): return float(self.io.query("MEASure:CURRent? CH1"))
    def close(self): self.io.close()


def ping(ip):
    flag = "-n" if platform.system().lower().startswith("win") else "-c"
    try:
        r = subprocess.run(["ping", flag, "1", ip], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=4)
        return r.returncode == 0
    except Exception:
        return False


def validate_2dp(p):
    if p in ("", ".", "-"): return True
    try: float(p)
    except ValueError: return False
    if "." in p and len(p.split(".", 1)[1]) > 2: return False
    return True


class Worker(threading.Thread):
    def __init__(self, ip, transport, out_q, cmd_q):
        super().__init__(daemon=True)
        self.ip, self.transport = ip, transport
        self.out_q, self.cmd_q = out_q, cmd_q
        self.psu = None
        self._stop = threading.Event()
    def run(self):
        try:
            self.psu = SPD(self.ip, self.transport)
            self.out_q.put(("connected", self.psu.idn()))
        except Exception as e:
            self.out_q.put(("error", f"Connect failed: {e}")); return
        nxt = time.time()
        while not self._stop.is_set():
            try:
                while True:
                    cmd, val = self.cmd_q.get_nowait(); self._handle(cmd, val)
            except queue.Empty:
                pass
            if time.time() >= nxt:
                nxt = time.time() + SAMPLE_INTERVAL
                try:
                    v = self.psu.measure_voltage(); i = self.psu.measure_current()
                    self.out_q.put(("sample", (datetime.now(), v, i, v * i)))
                except Exception as e:
                    self.out_q.put(("status", f"Read error: {e}"))
            time.sleep(0.05)
        try: self.psu.close()
        except Exception: pass
        self.out_q.put(("disconnected", None))
    def _handle(self, cmd, val):
        try:
            if cmd == "set_voltage":
                self.psu.set_voltage(val)
                self.out_q.put(("status", f"Voltage set to {val:.2f} V"))
            elif cmd == "set_current":
                self.psu.set_current(val)
                self.out_q.put(("status", f"Current limit set to {val:.2f} A"))
            elif cmd == "output":
                self.psu.output(val)
                self.out_q.put(("status", f"Output {'ON' if val else 'OFF'}"))
        except Exception as e:
            self.out_q.put(("status", f"Command error: {e}"))
    def stop(self): self._stop.set()


class App:
    def __init__(self, root, ip=None):
        self.root = root
        root.title("Siglent SPD1000X - LAN Control")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.out_q, self.cmd_q = queue.Queue(), queue.Queue()
        self.worker = None; self.connected = False
        self.logging = False; self.csv_file = None; self.csv_writer = None
        self.t0 = None; self.ts, self.vs, self.is_ = [], [], []
        self._build_ui()
        if ip: self.ip_var.set(ip)
        self.root.after(100, self._poll_queue)

    def _build_ui(self):
        vcmd = (self.root.register(validate_2dp), "%P")
        conn = ttk.LabelFrame(self.root, text="Network connection", padding=8)
        conn.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 0))
        ttk.Label(conn, text="IP address:").grid(row=0, column=0, sticky="e")
        self.ip_var = tk.StringVar(value="192.168.1.13")
        ttk.Entry(conn, textvariable=self.ip_var, width=18).grid(row=0, column=1, padx=5, sticky="w")
        ttk.Label(conn, text="Transport:").grid(row=0, column=2, sticky="e", padx=(10, 0))
        self.transport_var = tk.StringVar(value="VXI-11 (VISA)")
        ttk.Combobox(conn, textvariable=self.transport_var, width=14, state="readonly",
                     values=["VXI-11 (VISA)", "Socket (5025)"]).grid(row=0, column=3, padx=5)
        ttk.Button(conn, text="Ping test", command=self.on_ping).grid(row=0, column=4, padx=2)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=5, padx=2)

        c = ttk.Frame(self.root, padding=10); c.grid(row=1, column=0, sticky="nsew")
        ttk.Label(c, text="Voltage (V):").grid(row=0, column=0, sticky="e")
        self.v_entry = ttk.Entry(c, width=10, validate="key", validatecommand=vcmd)
        self.v_entry.grid(row=0, column=1, padx=5, pady=4); self.v_entry.insert(0, "0.00")
        ttk.Button(c, text="Set V", command=self.on_set_voltage).grid(row=0, column=2, padx=4)
        ttk.Label(c, text="Current (A):").grid(row=1, column=0, sticky="e")
        self.i_entry = ttk.Entry(c, width=10, validate="key", validatecommand=vcmd)
        self.i_entry.grid(row=1, column=1, padx=5, pady=4); self.i_entry.insert(0, "0.00")
        ttk.Button(c, text="Set I", command=self.on_set_current).grid(row=1, column=2, padx=4)
        ttk.Button(c, text="Output ON", command=lambda: self.on_output(True)).grid(row=2, column=1, pady=(8, 0))
        ttk.Button(c, text="Output OFF", command=lambda: self.on_output(False)).grid(row=2, column=2, pady=(8, 0))
        self.read_var = tk.StringVar(value="--- V   --- A   --- W")
        ttk.Label(c, text="Measured:").grid(row=3, column=0, sticky="e", pady=(8, 0))
        ttk.Label(c, textvariable=self.read_var, font=("TkDefaultFont", 10, "bold")).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.log_btn = ttk.Button(c, text="Start CSV Logging", command=self.on_toggle_logging)
        self.log_btn.grid(row=4, column=0, columnspan=2, pady=(10, 0), sticky="w")
        self.log_path_var = tk.StringVar(value="(not logging)")
        ttk.Label(c, textvariable=self.log_path_var).grid(row=5, column=0, columnspan=3, sticky="w")

        self.status_var = tk.StringVar(value="Enter the supply IP and click Connect.")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w").grid(row=2, column=0, columnspan=2, sticky="ew")

        self.fig = Figure(figsize=(6, 4), dpi=100)
        self.ax_v = self.fig.add_subplot(111); self.ax_i = self.ax_v.twinx()
        self.ax_v.set_xlabel("Time (s)"); self.ax_v.set_ylabel("Voltage (V)", color="tab:blue")
        self.ax_i.set_ylabel("Current (A)", color="tab:red")
        (self.line_v,) = self.ax_v.plot([], [], color="tab:blue")
        (self.line_i,) = self.ax_i.plot([], [], color="tab:red")
        self.fig.tight_layout()
        cf = ttk.Frame(self.root); cf.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        self.canvas = FigureCanvasTkAgg(self.fig, master=cf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.root.columnconfigure(1, weight=1); self.root.rowconfigure(1, weight=1)

    def on_ping(self):
        ip = self.ip_var.get().strip()
        if not ip: return
        self.status_var.set(f"Pinging {ip} ...")
        def work():
            ok = ping(ip)
            self.out_q.put(("status", f"Ping {ip}: reachable." if ok
                            else f"Ping {ip}: NO reply (check subnet / router isolation)."))
        threading.Thread(target=work, daemon=True).start()

    def toggle_connect(self):
        self.disconnect() if (self.connected or self.worker) else self.connect()
    def connect(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("No IP", "Enter the supply's IP address first."); return
        transport = "visa" if "VISA" in self.transport_var.get() else "socket"
        self.out_q, self.cmd_q = queue.Queue(), queue.Queue()
        self.worker = Worker(ip, transport, self.out_q, self.cmd_q)
        self.status_var.set(f"Connecting to {ip} ({transport}) ...")
        self.connect_btn.config(text="Cancel"); self.worker.start()
    def disconnect(self):
        if self.worker: self.worker.stop()
        self.status_var.set("Disconnecting...")
    def _set_connected_ui(self, on):
        self.connected = on
        self.connect_btn.config(text="Disconnect" if on else "Connect")

    def _require_connection(self):
        if not self.connected:
            messagebox.showwarning("Not connected", "Connect to the supply first."); return False
        return True
    def _read_field(self, entry, name):
        try: return round(float(entry.get()), 2)
        except ValueError:
            messagebox.showerror("Invalid input", f"Enter a valid {name} value (up to 2 decimals)."); return None
    def on_set_voltage(self):
        if not self._require_connection(): return
        v = self._read_field(self.v_entry, "voltage")
        if v is not None: self.cmd_q.put(("set_voltage", v))
    def on_set_current(self):
        if not self._require_connection(): return
        i = self._read_field(self.i_entry, "current")
        if i is not None: self.cmd_q.put(("set_current", i))
    def on_output(self, on):
        if not self._require_connection(): return
        self.cmd_q.put(("output", on))

    def on_toggle_logging(self):
        if not self.logging:
            default = f"spd1000x_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
            path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=default,
                                                filetypes=[("CSV files", "*.csv")])
            if not path: return
            self.csv_file = open(path, "w", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(["timestamp", "elapsed_s", "voltage_V", "current_A", "power_W"])
            self.csv_file.flush(); self.logging = True
            self.log_btn.config(text="Stop CSV Logging")
            self.log_path_var.set(f"Logging to: {os.path.basename(path)}")
        else:
            self._stop_logging()
    def _stop_logging(self):
        self.logging = False
        self.log_btn.config(text="Start CSV Logging"); self.log_path_var.set("(not logging)")
        if self.csv_file:
            try: self.csv_file.close()
            except Exception: pass
        self.csv_file = None; self.csv_writer = None

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "sample":
                    self._on_sample(*payload)
                elif kind == "connected":
                    self._set_connected_ui(True); self.status_var.set(f"Connected: {payload}")
                elif kind == "disconnected":
                    self._set_connected_ui(False); self.worker = None
                    self.status_var.set("Disconnected.")
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "error":
                    self._set_connected_ui(False); self.worker = None
                    self.status_var.set(payload); messagebox.showerror("Connection error", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_sample(self, ts, v, i, p):
        self.read_var.set(f"{v:.3f} V   {i:.3f} A   {p:.3f} W")
        if self.t0 is None: self.t0 = ts
        elapsed = (ts - self.t0).total_seconds()
        self.ts.append(elapsed); self.vs.append(v); self.is_.append(i)
        if len(self.ts) > MAX_POINTS:
            self.ts = self.ts[-MAX_POINTS:]; self.vs = self.vs[-MAX_POINTS:]; self.is_ = self.is_[-MAX_POINTS:]
        self.line_v.set_data(self.ts, self.vs); self.line_i.set_data(self.ts, self.is_)
        self.ax_v.relim(); self.ax_v.autoscale_view()
        self.ax_i.relim(); self.ax_i.autoscale_view()
        self.canvas.draw_idle()
        if self.logging and self.csv_writer:
            self.csv_writer.writerow([ts.isoformat(timespec="seconds"), f"{elapsed:.2f}",
                                      f"{v:.4f}", f"{i:.4f}", f"{p:.4f}"])
            self.csv_file.flush()

    def on_close(self):
        self._stop_logging()
        if self.worker: self.worker.stop()
        self.root.after(200, self.root.destroy)


def main():
    ap = argparse.ArgumentParser(description="LAN GUI for Siglent SPD1000X supply.")
    ap.add_argument("--ip", default=None, help="Supply IP address, e.g. 192.168.1.13")
    args = ap.parse_args()
    root = tk.Tk(); App(root, args.ip); root.mainloop()


if __name__ == "__main__":
    main()
