#!/usr/bin/env python3
"""Local bridge: reads the ESP32 over USB serial and streams it to a web page.

Run:  python server.py [--port /dev/cu.usbserial-0001] [--baud 921600]
Open: http://127.0.0.1:8765
"""
import argparse
import json
import math
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial  # pyserial, ships with the ESP-IDF Python env

HERE = Path(__file__).parent
ALLOWED_CMDS = ("wifi_config ", "radar ", "restart", "ping ")

clients: list[queue.Queue] = []
clients_lock = threading.Lock()
ser: serial.Serial | None = None
ser_lock = threading.Lock()
last_wifi_cmd: str | None = None  # kept in memory only, never written to disk
attached = threading.Event()       # cleared when the page asks to release the USB port
attached.set()


def send_later(cmd: str, delay: float) -> None:
    def run():
        time.sleep(delay)
        with ser_lock:
            if ser:
                ser.write((cmd + "\r\n").encode())
    threading.Thread(target=run, daemon=True).start()


def broadcast(kind: str, data: dict) -> None:
    msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
    with clients_lock:
        for q in list(clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # slow browser tab; drop frames rather than block


def parse_radar(parts: list[str]) -> dict | None:
    # RADAR_DADA,seq,ts,wander,wander_avg,someone_thr,someone,jitter,jitter_med,move_thr,move
    if len(parts) < 11:
        return None
    try:
        return {
            "seq": int(parts[1]),
            "wander": float(parts[3]),
            "someoneThr": float(parts[5]),
            "someone": parts[6].strip() == "1",
            "jitter": float(parts[7]),
            "moveThr": float(parts[9]),
            "moving": parts[10].strip() == "1",
        }
    except ValueError:
        return None


def parse_csi(line: str) -> dict | None:
    # CSI_DATA,...,rssi(6th field),...,"[i0,r0,i1,r1,...]"
    start, end = line.find('"['), line.rfind(']"')
    if start < 0 or end < 0:
        return None
    head = line[:start].split(",")
    try:
        raw = [int(v) for v in line[start + 2:end].split(",")]
        rssi = int(head[6])
    except (ValueError, IndexError):
        return None
    # ESP32 stores each subcarrier as (imaginary, real) int8 pairs
    amps = [round(math.hypot(raw[i], raw[i + 1]), 1) for i in range(0, len(raw) - 1, 2)]
    return {"rssi": rssi, "amps": amps}


def serial_loop(port: str, baud: int) -> None:
    global ser
    last_csi = 0.0
    last_rejoin = 0.0
    while True:
        if not attached.is_set():
            broadcast("status", {"serial": "released"})
            attached.wait()
        try:
            with ser_lock:
                # DTR/RTS low before opening, otherwise the board's auto-reset
                # circuit reboots it and it drops off WiFi
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, baud, 1
                ser.dtr = ser.rts = False
                ser.open()
            broadcast("status", {"serial": "connected", "port": port})
            buf = b""
            while True:
                if not attached.is_set():
                    with ser_lock:
                        ser.close()
                        ser = None
                    break
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if line.startswith("RADAR_DADA"):
                        d = parse_radar(line.split(","))
                        if d:
                            broadcast("radar", d)
                    elif line.startswith("CSI_DATA"):
                        now = time.time()
                        if now - last_csi >= 0.05:  # cap at ~20 fps for the browser
                            d = parse_csi(line)
                            if d:
                                broadcast("csi", d)
                                last_csi = now
                    elif "valid_len" not in line:  # skip per-packet debug spam
                        if "wifi_config -s" in line:
                            line = "wifi_config (connecting…)"  # don't echo the password to the page
                        broadcast("log", {"line": line[:300]})
                        if "app_main start" in line and last_wifi_cmd and time.time() - last_rejoin > 10:
                            last_rejoin = time.time()
                            # board rebooted (e.g. USB port reopened): rejoin WiFi
                            broadcast("log", {"line": "[server] board restarted, reconnecting WiFi"})
                            send_later(last_wifi_cmd, 3)
                        if "got ip" in line:
                            send_later("radar --csi_output_type LLTF", 0.5)
        except (serial.SerialException, OSError) as e:
            broadcast("status", {"serial": "disconnected", "error": str(e)})
            with ser_lock:
                ser = None
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=200)
            with clients_lock:
                clients.append(q)
            try:
                state = "released" if not attached.is_set() else ("connected" if ser else "waiting")
                self.wfile.write(f"event: status\ndata: {json.dumps({'serial': state})}\n\n".encode())
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = ": keepalive\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with clients_lock:
                    clients.remove(q)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/usb":
            # {"attach": false} frees the USB port (to unplug or flash); {"attach": true} takes it back
            try:
                length = int(self.headers.get("Content-Length", 0))
                attach = bool(json.loads(self.rfile.read(length))["attach"])
            except (ValueError, KeyError):
                return self.send_error(400)
            attached.set() if attach else attached.clear()
            self.send_response(204)
            self.end_headers()
            return
        if self.path != "/cmd":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            cmd = json.loads(self.rfile.read(length))["cmd"].strip()
        except (ValueError, KeyError):
            return self.send_error(400)
        if not cmd.startswith(ALLOWED_CMDS) or "\n" in cmd:
            return self.send_error(403, "command not allowed")
        global last_wifi_cmd
        with ser_lock:
            if not ser:
                return self.send_error(503, "board not connected")
            ser.write((cmd + "\r\n").encode())
        if cmd.startswith("wifi_config -s"):
            last_wifi_cmd = cmd
        elif cmd.startswith("wifi_config -d"):
            last_wifi_cmd = None  # user disconnected on purpose: don't auto-rejoin
        self.send_response(204)
        self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbserial-0001")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--http", type=int, default=8765)
    a = ap.parse_args()
    threading.Thread(target=serial_loop, args=(a.port, a.baud), daemon=True).start()
    # 127.0.0.1 only: the page can send your WiFi password to the board
    srv = ThreadingHTTPServer(("127.0.0.1", a.http), Handler)
    print(f"WiFi sensing page: http://127.0.0.1:{a.http}  (board: {a.port} @ {a.baud})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
