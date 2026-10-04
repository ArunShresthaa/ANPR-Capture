#!/usr/bin/env python3
"""
Hikvision DS-2CD1T43G2-I recorder with live preview.

Recording modes
  1. Original stream copy  - camera's main stream written untouched (best quality, small files)
  2. Lossless FFV1         - decoded and stored losslessly (large files)
  3. Raw YUV420P           - fully uncompressed frames (huge files)

Requirements
  pip install opencv-python pillow
  ffmpeg must be installed and available in PATH  (https://ffmpeg.org)

Preview uses a separate connection (sub stream by default) so it never
affects the recording, which always pulls the main stream (channel 101).
"""

import os
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import json
import shutil
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

import cv2
from PIL import Image, ImageTk

CONFIG_FILE = Path.home() / ".hikvision_recorder.json"

MODES = {
    "Original stream copy (no re-encode, recommended)": "copy",
    "Lossless FFV1 (decoded, large files)": "ffv1",
    "Raw YUV420P (uncompressed, HUGE files)": "raw",
}


def build_url(ip, port, user, password, channel):
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{ip}:{port}/Streaming/Channels/{channel}"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class PreviewThread(threading.Thread):
    """Grabs frames from RTSP in the background and keeps only the latest one."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        self.connected = False

    def run(self):
        while self.running:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                self.connected = False
                time.sleep(2)
                continue
            self.connected = True
            while self.running:
                ok, frame = cap.read()
                if not ok:
                    break
                with self.lock:
                    self.frame = frame
            self.connected = False
            cap.release()
            time.sleep(1)

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def stop(self):
        self.running = False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hikvision DS-2CD1T43G2-I Recorder")
        self.geometry("1100x760")
        self.minsize(900, 650)

        self.preview = None
        self.proc = None
        self.rec_start = None
        self.rec_dir = None
        self.err_lines = deque(maxlen=30)
        self.photo = None

        self._build_ui()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(33, self._update_frame)
        self.after(1000, self._update_stats)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        cfg = ttk.LabelFrame(self, text="Camera")
        cfg.pack(fill="x", padx=8, pady=6)

        self.ip = tk.StringVar(value="192.168.1.64")
        self.port = tk.StringVar(value="554")
        self.user = tk.StringVar(value="admin")
        self.pw = tk.StringVar()
        self.preview_main = tk.BooleanVar(value=False)

        for i, (label, var, show, w) in enumerate([
            ("IP", self.ip, None, 16), ("RTSP port", self.port, None, 6),
            ("User", self.user, None, 12), ("Password", self.pw, "*", 16),
        ]):
            ttk.Label(cfg, text=label).grid(row=0, column=i * 2, padx=(8, 2), pady=6, sticky="e")
            ttk.Entry(cfg, textvariable=var, show=show, width=w).grid(row=0, column=i * 2 + 1, padx=(0, 6))

        self.btn_connect = ttk.Button(cfg, text="Connect preview", command=self.toggle_preview)
        self.btn_connect.grid(row=0, column=8, padx=8)
        ttk.Checkbutton(cfg, text="Preview main stream (heavier)", variable=self.preview_main
                        ).grid(row=1, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 6))

        rec = ttk.LabelFrame(self, text="Recording")
        rec.pack(fill="x", padx=8, pady=6)

        self.mode = tk.StringVar(value=list(MODES)[0])
        self.outdir = tk.StringVar(value=str(Path.home() / "Videos" / "hikvision"))
        self.segment = tk.StringVar(value="10")
        self.audio = tk.BooleanVar(value=False)

        ttk.Label(rec, text="Mode").grid(row=0, column=0, padx=8, pady=6, sticky="e")
        ttk.Combobox(rec, textvariable=self.mode, values=list(MODES), state="readonly", width=48
                     ).grid(row=0, column=1, columnspan=3, sticky="w")

        ttk.Label(rec, text="Folder").grid(row=1, column=0, padx=8, pady=6, sticky="e")
        ttk.Entry(rec, textvariable=self.outdir, width=60).grid(row=1, column=1, columnspan=3, sticky="we")
        ttk.Button(rec, text="Browse", command=self._browse).grid(row=1, column=4, padx=6)

        ttk.Label(rec, text="Split every (min, 0 = single file)").grid(row=2, column=0, padx=8, pady=6, sticky="e")
        ttk.Spinbox(rec, from_=0, to=240, textvariable=self.segment, width=6).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(rec, text="Record audio (if camera has it)", variable=self.audio
                        ).grid(row=2, column=2, sticky="w", padx=8)

        self.btn_rec = ttk.Button(rec, text="● Start recording", command=self.toggle_record)
        self.btn_rec.grid(row=0, column=5, rowspan=3, padx=12, ipadx=10, ipady=8)

        self.canvas = tk.Label(self, bg="black", text="No preview", fg="#888")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=6)

        self.status = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken").pack(fill="x", side="bottom")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d:
            self.outdir.set(d)

    # ------------------------------------------------------------ config
    def _load_config(self):
        try:
            c = json.loads(CONFIG_FILE.read_text())
            self.ip.set(c.get("ip", self.ip.get()))
            self.port.set(c.get("port", self.port.get()))
            self.user.set(c.get("user", self.user.get()))
            self.outdir.set(c.get("outdir", self.outdir.get()))
        except Exception:
            pass

    def _save_config(self):
        try:
            CONFIG_FILE.write_text(json.dumps({
                "ip": self.ip.get(), "port": self.port.get(),
                "user": self.user.get(), "outdir": self.outdir.get()}))
        except Exception:
            pass

    # ----------------------------------------------------------- preview
    def toggle_preview(self):
        if self.preview:
            self.preview.stop()
            self.preview = None
            self.canvas.configure(image="", text="No preview")
            self.btn_connect.configure(text="Connect preview")
            return
        ch = 101 if self.preview_main.get() else 102
        url = build_url(self.ip.get(), self.port.get(), self.user.get(), self.pw.get(), ch)
        self.preview = PreviewThread(url)
        self.preview.start()
        self.btn_connect.configure(text="Disconnect preview")
        self._save_config()

    def _update_frame(self):
        if self.preview:
            frame = self.preview.get()
            if frame is not None:
                w = max(self.canvas.winfo_width(), 2)
                h = max(self.canvas.winfo_height(), 2)
                fh, fw = frame.shape[:2]
                scale = min(w / fw, h / fh)
                frame = cv2.resize(frame, (max(int(fw * scale), 1), max(int(fh * scale), 1)),
                                   interpolation=cv2.INTER_AREA)
                img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                self.photo = ImageTk.PhotoImage(img)
                self.canvas.configure(image=self.photo, text="")
            elif not self.preview.connected:
                self.canvas.configure(image="", text="Connecting...")
        self.after(33, self._update_frame)

    # --------------------------------------------------------- recording
    def _build_ffmpeg_cmd(self, url, outdir):
        mode = MODES[self.mode.get()]
        seg_min = int(float(self.segment.get() or 0))
        audio = self.audio.get()

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
               "-rtsp_transport", "tcp", "-fflags", "+genpts",
               "-i", url, "-map", "0:v:0"]
        if audio:
            cmd += ["-map", "0:a:0?"]

        if mode == "copy":
            cmd += ["-c:v", "copy"]
            if audio:
                cmd += ["-c:a", "copy"]
        elif mode == "ffv1":
            cmd += ["-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1",
                    "-g", "1", "-slices", "16", "-slicecrc", "1"]
            if audio:
                cmd += ["-c:a", "pcm_s16le"]
        else:  # raw
            cmd += ["-c:v", "rawvideo", "-pix_fmt", "yuv420p"]
            if audio:
                cmd += ["-c:a", "pcm_s16le"]

        if seg_min > 0:
            pattern = str(outdir / "cam_%Y%m%d_%H%M%S.mkv")
            cmd += ["-f", "segment", "-segment_time", str(seg_min * 60),
                    "-segment_format", "matroska", "-reset_timestamps", "1",
                    "-strftime", "1", pattern]
        else:
            name = datetime.now().strftime("cam_%Y%m%d_%H%M%S.mkv")
            cmd += [str(outdir / name)]
        return cmd

    def toggle_record(self):
        if self.proc:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        if not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg missing", "ffmpeg was not found in PATH. Install it first.")
            return
        mode = MODES[self.mode.get()]
        if mode == "raw" and not messagebox.askyesno(
                "Huge files",
                "Raw uncompressed 4MP video needs roughly 150 MB/s (~500 GB/hour).\nContinue?"):
            return

        outdir = Path(self.outdir.get())
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Folder error", str(e))
            return

        url = build_url(self.ip.get(), self.port.get(), self.user.get(), self.pw.get(), 101)
        try:
            cmd = self._build_ffmpeg_cmd(url, outdir)
        except ValueError:
            messagebox.showerror("Invalid value", "Split minutes must be a number.")
            return

        self.err_lines.clear()
        self.rec_dir = outdir
        self.rec_start = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        self.btn_rec.configure(text="■ Stop recording")
        self._save_config()

    def _read_stderr(self, proc):
        for line in proc.stderr:
            self.err_lines.append(line.rstrip())

    def stop_record(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.stdin.write("q\n")
                proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.terminate()
        self.btn_rec.configure(text="● Start recording")
        self.status.set(f"Stopped. Files saved in {self.rec_dir}")

    def _update_stats(self):
        if self.proc:
            if self.proc.poll() is not None:
                tail = "\n".join(list(self.err_lines)[-8:]) or "unknown error"
                self.proc = None
                self.btn_rec.configure(text="● Start recording")
                messagebox.showerror("Recording stopped", f"ffmpeg exited:\n\n{tail}")
            else:
                elapsed = int(time.time() - self.rec_start)
                size = sum(f.stat().st_size for f in self.rec_dir.glob("cam_*.mkv")
                           if f.stat().st_mtime >= self.rec_start - 2)
                free = shutil.disk_usage(self.rec_dir).free
                rate = size / max(elapsed, 1) / 1024 / 1024
                self.status.set(
                    f"● REC {elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}   "
                    f"size {human_size(size)}   rate {rate:.1f} MB/s   disk free {human_size(free)}")
        self.after(1000, self._update_stats)

    def _on_close(self):
        if self.proc:
            if not messagebox.askyesno("Recording", "Recording is active. Stop and exit?"):
                return
            self.stop_record()
        if self.preview:
            self.preview.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()