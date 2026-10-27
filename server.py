#!/usr/bin/env python3
"""Local Video Deck helper.

Serves the web page and runs what a browser can't: converting with ffmpeg and
enhancing with Real-ESRGAN, one step at a time so progress can be reported.
Listens on 127.0.0.1 only. Start it with ./start.sh, which installs anything missing.

Environment:
  PORT             port to listen on (default 8765)
  VIDEO_DECK_GPU   Vulkan device number for Real-ESRGAN (default: picked automatically)
"""
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
SETTINGS_FILE = HERE / "settings.json"
ESRGAN = HERE / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan"
WORK_ROOT = HERE / ".work"  # frames can take many GB, so they stay next to the app, not in RAM-backed /tmp
HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8765"))
URL = f"http://{HOST}:{PORT}/"

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts", ".m2ts", ".wmv", ".flv"}
MP4_LIKE = {".mp4", ".mov", ".m4v"}
HEIGHTS = {2160, 1440, 1080, 720, 480}
QUALITY_CQ = {
    "convert": {"best": 19, "balanced": 23, "small": 28},
    "enhance": {"best": 16, "balanced": 19, "small": 24},
}
ENHANCE_WEIGHTS = (0.05, 0.90, 0.05)  # extract, upscale, encode: upscaling is nearly all the time
ENCODER_CANDIDATES = {  # first one that works on this machine wins
    "h264": ["h264_nvenc", "libx264", "libopenh264"],
    "hevc": ["hevc_nvenc", "libx265"],
}

# Filled in by detect_hardware() at startup.
HW = {"encoders": {}, "gpu": None, "gpuName": "", "enhanceProblem": "", "convertProblem": ""}


# ---------- hardware detection ----------
def detect_encoders():
    found = {}
    for codec, names in ENCODER_CANDIDATES.items():
        for name in names:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
                 "-frames:v", "3", "-c:v", name, "-f", "null", "-"],
                capture_output=True, timeout=60)
            if r.returncode == 0:
                found[codec] = name
                break
    return found


def gpu_score(name):
    """Dedicated cards first, integrated graphics next, software renderers last."""
    n = name.lower()
    if "llvmpipe" in n or "swiftshader" in n or "lavapipe" in n:
        return 0
    if "nvidia" in n or re.search(r"radeon\s*(rx|pro|vii)|arc(\(tm\))?\s*a\d", n):
        return 3
    return 1


def detect_gpu():
    if not ESRGAN.exists():
        return None, "", "Real-ESRGAN isn't installed. Run ./start.sh to download it."
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK_ROOT) as d:
        img = Path(d) / "probe.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=gray:s=32x32", "-frames:v", "1", str(img)],
                       check=True, timeout=30)
        r = subprocess.run([str(ESRGAN), "-i", str(img), "-o", str(Path(d) / "out.png"),
                            "-n", "realesr-animevideov3", "-s", "2"],
                           capture_output=True, text=True, timeout=180)
    devices = {}
    for m in re.finditer(r"^\[(\d+) (.+?)\]\s", r.stderr, re.M):
        devices[int(m.group(1))] = m.group(2)
    forced = os.environ.get("VIDEO_DECK_GPU", "").strip()
    if forced:
        return forced, devices.get(int(forced), f"device {forced}") if forced.isdigit() else forced, ""
    if not devices:
        return None, "", ("No graphics card with Vulkan support was found, so Enhance can't run. "
                          "Install or update your graphics driver, then restart the app.")
    best = max(devices, key=lambda i: (gpu_score(devices[i]), -i))
    return str(best), devices[best], ""


def detect_hardware():
    HW["encoders"] = detect_encoders()
    if "h264" not in HW["encoders"]:
        HW["convertProblem"] = ("Your ffmpeg can't encode H.264 video. Install the full ffmpeg package "
                                "(on Fedora it comes from RPM Fusion), then restart the app.")
    try:
        HW["gpu"], HW["gpuName"], HW["enhanceProblem"] = detect_gpu()
    except (OSError, subprocess.SubprocessError) as e:
        HW["gpu"], HW["gpuName"], HW["enhanceProblem"] = None, "", f"Real-ESRGAN couldn't start: {e}"
    if not HW["enhanceProblem"] and HW["convertProblem"]:
        HW["enhanceProblem"] = HW["convertProblem"]


def video_args(codec, kind, quality, w, h, fps, mp4):
    """Encoder flags for the best available encoder of `codec`, falling back to H.264."""
    name = HW["encoders"].get(codec) or HW["encoders"]["h264"]
    cq = QUALITY_CQ[kind][quality]
    if name.endswith("_nvenc"):
        args = ["-c:v", name, "-preset", "p6", "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    elif name == "libx264":
        args = ["-c:v", name, "-preset", "medium", "-crf", str(cq)]
    elif name == "libx265":
        args = ["-c:v", name, "-preset", "medium", "-crf", str(cq + 2), "-x265-params", "log-level=error"]
    else:  # libopenh264 has no quality mode, so pick a bitrate from the picture size
        bpp = {"best": 0.2, "balanced": 0.12, "small": 0.07}[quality]
        args = ["-c:v", name, "-b:v", str(int(w * h * max(fps, 1) * bpp))]
    if mp4 and name in ("hevc_nvenc", "libx265"):
        args += ["-tag:v", "hvc1"]  # so Apple devices recognise H.265 in MP4
    return args


def encoder_info():
    def describe(name):
        if not name:
            return None
        return {"name": name, "gpu": name.endswith("_nvenc"),
                "format": "H.265" if name in ("hevc_nvenc", "libx265") else "H.264"}
    enc = HW["encoders"]
    return {"h264": describe(enc.get("h264")), "hevc": describe(enc.get("hevc")),
            "enhance": describe(enc.get("hevc") or enc.get("h264"))}


# ---------- settings: remembered folders and options ----------
_settings_lock = threading.RLock()


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def update_settings(patch):
    with _settings_lock:
        s = load_settings()
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        tmp.replace(SETTINGS_FILE)
        return s


def remember_destination(kind, folder):
    """'' means "next to the original video"."""
    with _settings_lock:
        patch = {"dest": {kind: folder}}
        if folder:
            recent = load_settings().get("recent", {}).get(kind, [])
            patch["recent"] = {kind: ([folder] + [p for p in recent if p != folder])[:6]}
        return update_settings(patch)


# ---------- media helpers ----------
def probe(path):
    if not os.path.isfile(path):
        raise ValueError("That file doesn't exist any more.")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,codec_name:format=duration,size",
         "-of", "json", path],
        capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {}
    if r.returncode or not data.get("streams"):
        raise ValueError("This file has no video that ffmpeg can read.")
    s, f = data["streams"][0], data.get("format", {})
    rate = s.get("r_frame_rate") or "30/1"
    num, _, den = rate.partition("/")
    fps = float(num) / float(den) if den and float(den) else float(num or 30)
    duration = float(f.get("duration") or 0)
    frames = int(s.get("nb_frames") or 0) or round(duration * fps)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    return {
        "path": path, "name": os.path.basename(path), "dir": os.path.dirname(path),
        "width": int(s["width"]), "height": int(s["height"]), "fps": fps, "rate": rate,
        "duration": duration, "frames": frames, "codec": s.get("codec_name", ""),
        "size": int(f.get("size") or os.path.getsize(path)),
        "workFree": shutil.disk_usage(WORK_ROOT).free,
    }


def places():
    home = Path.home()
    found = [("Home", home)] + [(n, home / n) for n in ("Videos", "Downloads", "Desktop")]
    user = os.environ.get("USER", "")
    for root in (Path("/mnt"), Path("/media") / user, Path("/run/media") / user):
        try:
            found += [(d.name, d) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith(".")]
        except OSError:
            pass
    return [{"name": n, "path": str(p)} for n, p in found if p.is_dir()]


def browse(path, want_files):
    start = path or load_settings().get("lastInputDir") or str(Path.home())
    p = Path(start).expanduser()
    while not p.is_dir() and p != p.parent:
        p = p.parent
    dirs, files = [], []
    try:
        entries = sorted(os.scandir(p), key=lambda e: e.name.lower())
    except PermissionError:
        raise ValueError("You don't have permission to open this folder.")
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            elif want_files and Path(e.name).suffix.lower() in VIDEO_EXT:
                files.append({"name": e.name, "size": e.stat().st_size})
        except OSError:
            pass
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None,
            "dirs": dirs, "files": files, "places": places()}


def output_plan(kind, info, opts):
    """Output size and file name; the page computes the same thing for its preview."""
    src = Path(info["path"])
    ext = ".mp4" if src.suffix.lower() in MP4_LIKE else ".mkv"
    if kind == "convert":
        h = opts["height"]
        w = int(info["width"] * h / info["height"] / 2 + 0.5) * 2
        return w, h, f"{src.stem}_{h}p{ext}"
    s = opts["scale"]
    return info["width"] * s // 2 * 2, info["height"] * s // 2 * 2, f"{src.stem}_enhanced_x{s}{ext}"


# ---------- jobs ----------
class Canceled(Exception):
    pass


jobs = {}               # id -> Job, oldest first
job_queue = queue.Queue()
_reserved = set()       # outputs of unfinished jobs, so two queued jobs never share a name
_reserve_lock = threading.Lock()


def unique_output(folder, name):
    stem, ext = os.path.splitext(name)
    with _reserve_lock:
        p, n = Path(folder) / name, 2
        while p.exists() or str(p) in _reserved:
            p, n = Path(folder) / f"{stem} ({n}){ext}", n + 1
        _reserved.add(str(p))
        return str(p)


class Job:
    def __init__(self, kind, info, dest, opts):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.info, self.opts = kind, info, opts
        self.width, self.height, name = output_plan(kind, info, opts)
        self.output = unique_output(dest, name)
        names = ["Convert"] if kind == "convert" else ["Extract frames", "Upscale frames", "Encode video"]
        self.stages = [{"name": n, "state": "waiting", "detail": ""} for n in names]
        self.weights = (1.0,) if kind == "convert" else ENHANCE_WEIGHTS
        self.stage = 0
        self.status, self.percent, self.eta, self.detail, self.error = "queued", 0.0, None, "", ""
        self.created, self.started, self.finished = time.time(), None, None
        self.proc, self.canceled = None, False

    def set_stage(self, i):
        for k, st in enumerate(self.stages):
            st["state"] = "done" if k < i else "running" if k == i else "waiting"
        self.stage, self.eta, self.detail = i, None, ""
        self.percent = sum(self.weights[:i])

    def progress(self, frac, detail="", eta=None):
        frac = max(0.0, min(frac, 1.0))
        self.percent = sum(self.weights[:self.stage]) + self.weights[self.stage] * frac
        self.stages[self.stage]["detail"] = detail
        self.detail, self.eta = detail, eta

    def to_dict(self):
        return {
            "id": self.id, "type": self.kind, "status": self.status,
            "input": self.info["path"], "output": self.output,
            "spec": f"{self.width} × {self.height}", "stages": self.stages, "stage": self.stage,
            "percent": self.percent, "eta": self.eta, "detail": self.detail, "error": self.error,
            "created": self.created, "started": self.started, "finished": self.finished,
        }


def start_proc(job, cmd, **kw):
    if job.canceled:
        raise Canceled()
    job.proc = subprocess.Popen(cmd, start_new_session=True, **kw)
    return job.proc


def check_proc(job, proc, errfile, what):
    if job.canceled:
        raise Canceled()
    if proc.returncode:
        errfile.seek(0)
        lines = [l for l in errfile.read().strip().splitlines() if l.strip()]
        raise RuntimeError(f"{what} stopped with an error: {lines[-1] if lines else f'exit code {proc.returncode}'}")


def run_ffmpeg(job, args, on_progress, what="ffmpeg"):
    with tempfile.TemporaryFile("w+") as err:
        proc = start_proc(job, ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args],
                          stdout=subprocess.PIPE, stderr=err, text=True)
        block = {}
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            block[k] = v
            if k == "progress":
                on_progress(block)
                block = {}
        proc.wait()
        check_proc(job, proc, err, what)


def _num(v, default=0.0):
    try:
        return float(str(v).rstrip("x"))
    except ValueError:
        return default


def convert(job):
    i, o = job.info, job.opts
    mkv = job.output.endswith(".mkv")
    duration = i["duration"]
    job.set_stage(0)

    def progress(b):
        t = _num(b.get("out_time_us"), 0) / 1e6
        speed = _num(b.get("speed"), 0)
        eta = (duration - t) / speed if speed > 0 and duration else None
        detail = f"{_num(b.get('fps')):.0f} frames a second, {speed:.1f}× real time" if speed else ""
        job.progress(t / duration if duration else 0, detail, eta)

    run_ffmpeg(job, [
        "-i", i["path"], "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
        *(["-map", "0:t?", "-c:t", "copy"] if mkv else []),
        "-vf", f"scale={job.width}:{job.height}:flags=lanczos,format=yuv420p",
        *video_args(o["codec"], "convert", o["quality"], job.width, job.height, i["fps"], not mkv),
        "-c:a", "copy", "-c:s", "copy", job.output,
    ], progress, "ffmpeg")


def enhance(job):
    i, o = job.info, job.opts
    if o["model"] == "anime":
        model, rscale = "realesr-animevideov3", o["scale"]
    else:
        model, rscale = "realesrgan-x4plus", 4  # always x4, scaled down to the chosen size when encoding
    mkv = job.output.endswith(".mkv")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="job.", dir=WORK_ROOT))
    count = lambda d: sum(1 for n in os.listdir(d) if n.endswith(".png"))
    try:
        fin, fout = work / "in", work / "out"
        fin.mkdir()
        fout.mkdir()

        job.set_stage(0)
        expected = i["frames"]

        def p_extract(b):
            n = int(_num(b.get("frame"), 0))
            job.progress(n / expected if expected else 0, f"{n:,} of about {expected:,} frames")

        run_ffmpeg(job, ["-i", i["path"], "-map", "0:v:0", "-fps_mode", "passthrough", str(fin / "%08d.png")],
                   p_extract, "Frame extraction")
        total = count(fin)
        if not total:
            raise RuntimeError("No frames could be read from this video.")

        job.set_stage(1)
        with tempfile.TemporaryFile("w+") as err:
            proc = start_proc(job, [
                str(ESRGAN), "-i", str(fin), "-o", str(fout), "-n", model, "-s", str(rscale), "-g", HW["gpu"],
                "-t", "256" if o["lowMemory"] else "0", "-f", "png", "-j", "2:2:2",
            ], stdout=subprocess.DEVNULL, stderr=err)
            samples = []
            while proc.poll() is None:
                done, now = count(fout), time.time()
                samples = (samples + [(now, done)])[-60:]  # rate over the last minute
                t0, d0 = samples[0]
                rate = (done - d0) / (now - t0) if now > t0 else 0
                eta = (total - done) / rate if rate > 0 else None
                speed = f", {rate:.2f} frames a second" if rate else ""
                job.progress(done / total, f"{done:,} of {total:,} frames{speed}", eta)
                time.sleep(1)
            check_proc(job, proc, err, "Real-ESRGAN")
        done = count(fout)
        if done < total:
            raise RuntimeError(f"Only {done:,} of {total:,} frames were upscaled. "
                               "Turn on “Use less graphics memory” and try again.")
        shutil.rmtree(fin, ignore_errors=True)  # free space before encoding

        job.set_stage(2)

        def p_encode(b):
            n = int(_num(b.get("frame"), 0))
            speed = _num(b.get("fps"), 0)
            eta = (total - n) / speed if speed > 0 else None
            job.progress(n / total, f"{n:,} of {total:,} frames", eta)

        run_ffmpeg(job, [
            "-framerate", i["rate"], "-i", str(fout / "%08d.png"), "-i", i["path"],
            "-map", "0:v", "-map", "1:a?", "-map", "1:s?", *(["-map", "1:t?", "-c:t", "copy"] if mkv else []),
            "-c:a", "copy", "-c:s", "copy",
            "-vf", f"scale={job.width}:{job.height}:flags=lanczos,format=yuv420p",
            *video_args("hevc", "enhance", o["quality"], job.width, job.height, i["fps"], not mkv),
            job.output,
        ], p_encode, "Encoding")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def worker():
    while True:
        job = job_queue.get()
        if job.canceled:
            continue
        job.status, job.started = "running", time.time()
        try:
            (convert if job.kind == "convert" else enhance)(job)
            job.status, job.percent, job.eta, job.detail = "done", 1.0, None, ""
            for st in job.stages:
                st["state"] = "done"
        except Canceled:
            job.status = "canceled"
        except Exception as e:  # report any failure on the job card
            job.status, job.error = "failed", str(e)
        finally:
            job.finished, job.proc = time.time(), None
            if job.status != "done":
                for st in job.stages:
                    if st["state"] == "running":
                        st["state"] = "stopped"
                try:
                    os.unlink(job.output)
                except OSError:
                    pass
            with _reserve_lock:
                _reserved.discard(job.output)


def cancel(job):
    job.canceled = True
    if job.status == "queued":
        job.status, job.finished = "canceled", time.time()
        with _reserve_lock:
            _reserved.discard(job.output)
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def new_job(d):
    kind = d.get("type")
    if kind not in ("convert", "enhance"):
        raise ValueError("Unknown job type.")
    problem = HW["convertProblem"] if kind == "convert" else HW["enhanceProblem"]
    if problem:
        raise ValueError(problem)
    info = probe(str(d.get("input") or ""))
    o = d.get("options") or {}
    if kind == "convert":
        opts = {"height": int(o.get("height", 1080)), "codec": o.get("codec", "h264"),
                "quality": o.get("quality", "balanced")}
        if opts["height"] not in HEIGHTS or opts["codec"] not in ("h264", "hevc"):
            raise ValueError("Choose a size and a format.")
        if opts["codec"] == "hevc" and "hevc" not in HW["encoders"]:
            raise ValueError("Your ffmpeg can't encode H.265. Choose H.264 instead.")
    else:
        opts = {"model": o.get("model", "real"), "scale": int(o.get("scale", 2)),
                "quality": o.get("quality", "balanced"), "lowMemory": bool(o.get("lowMemory"))}
        if opts["model"] not in ("real", "anime") or opts["scale"] not in (2, 3, 4):
            raise ValueError("Choose a footage type and a size.")
    if opts["quality"] not in QUALITY_CQ[kind]:
        raise ValueError("Choose a quality.")
    dest = d.get("dest") or info["dir"]
    if not os.path.isdir(dest):
        raise ValueError("The save folder doesn't exist any more. Choose another one.")
    if not os.access(dest, os.W_OK):
        raise ValueError("You can't save files in that folder. Choose another one.")
    job = Job(kind, info, dest, opts)
    jobs[job.id] = job
    job_queue.put(job)
    return job


# ---------- HTTP ----------
STATIC = {"/": "index.html", "/index.html": "index.html", "/icon.svg": "icon.svg"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host_ok(self):
        # Blocks other websites reaching this helper through DNS rebinding.
        return self.headers.get("Host", "") in {f"{HOST}:{PORT}", f"localhost:{PORT}"}

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, fn):
        if not self._host_ok():
            return self.send_error(403)
        try:
            fn()
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": f"Something went wrong in the helper: {e}"}, 500)

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        # Requiring JSON makes browsers preflight cross-site requests, which this server never approves.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_error(415)
        self._handle(self._post)

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in STATIC:
            f = WEB / STATIC[u.path]
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(f.name)[0] or "text/html") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/status":
            self.send_json({"ok": True, "gpuName": HW["gpuName"], "encoders": encoder_info(),
                            "convertProblem": HW["convertProblem"], "enhanceProblem": HW["enhanceProblem"]})
        elif u.path == "/api/settings":
            self.send_json(load_settings())
        elif u.path == "/api/browse":
            self.send_json(browse(q.get("path", ""), q.get("files") == "1"))
        elif u.path == "/api/probe":
            self.send_json(probe(q.get("path", "")))
        elif u.path == "/api/jobs":
            self.send_json({"jobs": [j.to_dict() for j in reversed(list(jobs.values()))]})
        elif u.path == "/api/file":
            self.send_video(q.get("path", ""))
        else:
            self.send_error(404)

    def _post(self):
        length = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path
        if path == "/api/settings":
            s = load_settings()
            for kind, folder in (d.get("dest") or {}).items():
                if kind in ("convert", "enhance"):
                    s = remember_destination(kind, folder or "")
            patch = {k: d[k] for k in ("options", "lastInputDir") if k in d}
            if patch:
                s = update_settings(patch)
            self.send_json(s)
        elif path == "/api/jobs":
            self.send_json(new_job(d).to_dict())
        elif path.startswith("/api/jobs/"):
            _, _, _, jid, action = (path.split("/") + [""])[:5]
            job = jobs.get(jid)
            if not job:
                raise ValueError("That job no longer exists.")
            if action == "cancel":
                cancel(job)
            elif action == "remove" and job.status in ("done", "failed", "canceled"):
                jobs.pop(jid, None)
            self.send_json({"ok": True})
        elif path == "/api/reveal":
            p = Path(d.get("path", ""))
            folder = p if p.is_dir() else p.parent
            if not folder.is_dir():
                raise ValueError("That folder doesn't exist any more.")
            subprocess.Popen(["xdg-open", str(folder)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_json({"ok": True})
        elif path == "/api/mkdir":
            name = str(d.get("name", "")).strip()
            if not name or "/" in name or name in (".", ".."):
                raise ValueError("Folder names can't be empty or contain “/”.")
            target = Path(d.get("path", "")) / name
            try:
                target.mkdir()
            except FileExistsError:
                raise ValueError("A folder with that name already exists here.")
            except PermissionError:
                raise ValueError("You can't create folders here.")
            self.send_json({"path": str(target)})
        else:
            self.send_error(404)

    def send_video(self, path):
        p = Path(path)
        if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
            return self.send_error(404)
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            if a:
                start, end = int(a), int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    want_open = "--open" in sys.argv
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Local Video Deck is already running at {URL}")
        if want_open:
            webbrowser.open(URL)
        return
    server.daemon_threads = True
    print("Checking your graphics card and video encoders…", flush=True)
    detect_hardware()
    enc = HW["encoders"]
    print(f"  Converting with: {enc.get('h264', 'nothing (see the Convert tab)')}"
          f"{' and ' + enc['hevc'] if 'hevc' in enc else ''}")
    print(f"  Enhancing on:    {HW['gpuName'] or HW['enhanceProblem']}")
    threading.Thread(target=worker, daemon=True).start()
    print(f"\nLocal Video Deck is running at {URL}\nKeep this window open while you use it. Press Ctrl+C to stop.",
          flush=True)
    if want_open:
        threading.Timer(0.5, webbrowser.open, [URL]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping. Unfinished jobs are canceled.")
    finally:
        for job in list(jobs.values()):
            if job.status in ("queued", "running"):
                cancel(job)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
