"""GLB2VRM - a local web UI that turns a rigged GLB into a VRM avatar.

Run:
    python glb2vrm/server.py            # then open http://127.0.0.1:8020
    python -m glb2vrm.server --port 8020
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from glb2vrm import convert as conv  # noqa: E402
from glb2vrm import mapping as mapping_mod  # noqa: E402
from glb2vrm.glb import GLBError, read_glb  # noqa: E402

WEB = HERE / "web"
VENDOR = ROOT / "ui" / "viewer" / "vendor"
DATA = HERE / "data"
MODELS = DATA / "models"
JOBS_DIR = DATA / "jobs"

MAX_UPLOAD = 512 * 1024 * 1024
TOKEN = re.compile(r"[^A-Za-z0-9._ -]+")

jobs: dict[str, "Job"] = {}
jobs_lock = threading.Lock()


def clean_name(name: str, default: str = "model") -> str:
    stem = TOKEN.sub("_", Path(name).name).strip(" ._")
    return stem or default


def safe_json(value):
    return json.dumps(value, ensure_ascii=False)


def pretty_bytes(size: int) -> str:
    if size >= 1 << 30:
        return f"{size / (1 << 30):.2f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    if size >= 1 << 10:
        return f"{size / (1 << 10):.1f} KB"
    return f"{size} B"


# ------------------------------------------------------------------- models
def model_path(name: str) -> Path:
    return MODELS / Path(name).name


def model_info(path: Path):
    doc = read_glb(path)
    info = doc.model_info()
    guessed = mapping_mod.detect(info.get("skeleton"))
    info["suggested"] = guessed
    info["bytes_human"] = pretty_bytes(info["stats"]["bytes"])
    info["url"] = f"/api/models/{path.name}"
    return info


def list_models():
    MODELS.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(MODELS.glob("*")):
        if path.suffix.lower() not in (".glb", ".vrm"):
            continue
        items.append({"name": path.name, "bytes": path.stat().st_size,
                      "bytes_human": pretty_bytes(path.stat().st_size),
                      "url": f"/api/models/{path.name}"})
    return items


# ---------------------------------------------------------------------- job
class Job:
    def __init__(self, model: Path, mapping: dict, options: dict):
        self.id = uuid.uuid4().hex[:10]
        self.model = model
        self.mapping = mapping
        self.options = options
        self.work = JOBS_DIR / self.id
        self.started = time.time()
        self._lock = threading.Lock()
        self._status = {"state": "queued", "phase": "queued",
                        "message": "starting", "error": "", "elapsed": 0.0,
                        "log": [], "result": None}
        self._thread = None

    def set(self, **fields):
        with self._lock:
            self._status.update(fields)
            if "log" in fields:
                del self._status["log"][:-120]

    def add_log(self, lines):
        with self._lock:
            self._status["log"].extend(lines)
            del self._status["log"][:-160]

    def finish(self, result):
        with self._lock:
            self._status.update(state="done", phase="done",
                                message="VRM ready", result=result,
                                elapsed=round(time.time() - self.started, 1))

    def fail(self, message):
        with self._lock:
            self._status.update(state="error", phase="failed",
                                error=str(message)[-4000:],
                                elapsed=round(time.time() - self.started, 1))

    def snapshot(self):
        with self._lock:
            data = dict(self._status)
            data["log"] = list(self._status["log"])[-60:]
        data["id"] = self.id
        if data["state"] == "running":
            data["elapsed"] = round(time.time() - self.started, 1)
        return data

    def _save(self):
        """Persist the finished job so it survives a server restart."""
        try:
            self.work.mkdir(parents=True, exist_ok=True)
            snapshot = self.snapshot()
            (self.work / "job.json").write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.work.mkdir(parents=True, exist_ok=True)
        self.set(state="running", phase="blender",
                 message="starting Blender")
        try:
            def report(phase, message):
                self.set(phase=phase, message=message)

            def sink(line):
                self.add_log([line])

            self.set(**{"log": []})
            result = conv.convert_model(self.model, self.mapping, self.options,
                                        self.work, report, sink)
            result.pop("log", None)
            self.finish({
                "name": result["name"],
                "url": f"/api/jobs/{self.id}/file",
                "bytes": result["bytes"],
                "bytes_human": pretty_bytes(result["bytes"]),
                "summary": result["summary"],
                "valid": result["valid"],
                "blender": result["blender"],
            })
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.fail(str(exc))
        finally:
            self._save()


def load_saved_job(job_id: str):
    """A job from a previous server process, read from data/jobs/<id>/job.json."""
    folder = JOBS_DIR / Path(job_id).name
    try:
        data = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data["id"] = job_id
    return data


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GLB2VRM/1.0"

    # -- output helpers
    def _send_json(self, value, code=200):
        body = safe_json(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message, code=400):
        self._send_json({"error": message}, code)

    def _send_file(self, path: Path, download=False, cache=False):
        if not path.exists() or not path.is_file():
            return self._send_error_json("not found", 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in (".glb", ".vrm"):
            ctype = "model/gltf-binary"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store" if not cache else "public, max-age=3600")
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        with path.open("rb") as handle:
            remaining = size
            while remaining > 0:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        if length > MAX_UPLOAD:
            raise ValueError("file is too large")
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # -- routing
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/status":
            return self._send_json({"status": conv.status(), "models": len(list_models())})

        if path == "/api/models":
            return self._send_json({"models": list_models()})

        if path.startswith("/api/models/"):
            rest = path[len("/api/models/"):]
            if rest.endswith("/info"):
                name = rest[:-len("/info")]
                target = model_path(name)
                if not target.exists():
                    return self._send_error_json("model not found", 404)
                try:
                    return self._send_json(model_info(target))
                except GLBError as exc:
                    return self._send_error_json(str(exc), 400)
                except Exception as exc:  # noqa: BLE001
                    return self._send_error_json(f"could not read the model: {exc}", 400)
            target = model_path(rest)
            return self._send_file(target, cache=True)

        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):]
            if rest.endswith("/file"):
                job_id = Path(rest[:-len("/file")]).name
                with jobs_lock:
                    job = jobs.get(job_id)
                if job is not None:
                    snapshot = job.snapshot()
                    if snapshot["state"] != "done":
                        return self._send_error_json("the VRM is not ready yet", 409)
                    name = snapshot["result"]["name"]
                else:
                    saved = load_saved_job(job_id)
                    if not saved or saved.get("state") != "done":
                        return self._send_error_json("unknown job", 404)
                    name = (saved.get("result") or {}).get("name")
                    if not name:
                        return self._send_error_json("unknown job", 404)
                return self._send_file(JOBS_DIR / job_id / Path(name).name,
                                       download=query.get("download", ["0"])[0] == "1")
            job_id = Path(rest).name
            with jobs_lock:
                job = jobs.get(job_id)
            if job is not None:
                return self._send_json(job.snapshot())
            saved = load_saved_job(job_id)
            if saved is None:
                return self._send_error_json("unknown job", 404)
            return self._send_json(saved)

        # static UI
        if path in ("/", "/index.html"):
            return self._send_file(WEB / "index.html")
        if path.startswith("/vendor/"):
            rel = path[len("/vendor/"):]
            target = (VENDOR / rel).resolve()
            try:
                target.relative_to(VENDOR.resolve())
            except ValueError:
                return self._send_error_json("not found", 404)
            return self._send_file(target, cache=True)
        target = (WEB / path.lstrip("/")).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._send_error_json("not found", 404)
        return self._send_file(target)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/models":
            name = clean_name((query.get("name") or ["model.glb"])[0])
            if not name.lower().endswith((".glb", ".vrm")):
                name += ".glb"
            try:
                body = self._read_body()
            except ValueError as exc:
                return self._send_error_json(str(exc), 413)
            if len(body) < 20:
                return self._send_error_json("the file is empty or too small", 400)
            if body[:4] != b"glTF":
                return self._send_error_json("not a GLB file", 400)
            if len(body) > MAX_UPLOAD:
                return self._send_error_json("file is too large", 413)
            MODELS.mkdir(parents=True, exist_ok=True)
            destination = MODELS / name
            digest = hashlib.sha256(body).hexdigest()
            if destination.exists():
                try:
                    same = hashlib.sha256(destination.read_bytes()).hexdigest() == digest
                except OSError:
                    same = False
                if not same:
                    destination = MODELS / f"{Path(name).stem}_{digest[:6]}{Path(name).suffix}"
            destination.write_bytes(body)
            try:
                info = model_info(destination)
            except GLBError as exc:
                destination.unlink(missing_ok=True)
                return self._send_error_json(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                destination.unlink(missing_ok=True)
                return self._send_error_json(f"could not read the model: {exc}", 400)
            return self._send_json({"model": info["name"], "info": info})

        if path == "/api/convert":
            try:
                payload = json.loads(self._read_body().decode("utf-8") or "{}")
            except ValueError:
                return self._send_error_json("invalid JSON body", 400)
            name = str(payload.get("model") or "")
            target = model_path(name)
            if not target.exists():
                return self._send_error_json("model not found", 400)
            mapping = payload.get("mapping") or {}
            if not isinstance(mapping, dict):
                return self._send_error_json("mapping must be an object", 400)
            cleaned = {}
            for slot, bone in mapping.items():
                if slot not in mapping_mod.SLOT_NAMES or not bone:
                    continue
                cleaned[str(slot)] = str(bone)
            missing = [slot for slot in mapping_mod.REQUIRED if slot not in cleaned]
            if missing:
                return self._send_error_json(
                    "map the required humanoid bones first: " + ", ".join(missing), 400)
            options = payload.get("options") or {}
            available = conv.status()
            if not available["ok"]:
                return self._send_error_json(available["reason"], 409)
            with jobs_lock:
                running = [j for j in jobs.values()
                           if j.snapshot()["state"] in ("queued", "running")]
                if running:
                    return self._send_error_json("a conversion is already running", 409)
                job = Job(target, cleaned, options)
                jobs[job.id] = job
            job.start()
            return self._send_json({"id": job.id, "job": job.snapshot()})

        if path == "/api/clear":
            try:
                payload = json.loads(self._read_body().decode("utf-8") or "{}")
            except ValueError:
                return self._send_error_json("invalid JSON body", 400)
            name = str(payload.get("model") or "")
            target = MODELS / Path(name).name
            if target.exists():
                target.unlink()
            return self._send_json({"ok": True})

        return self._send_error_json("not found", 404)

    def log_message(self, fmt, *args):  # quieter console
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError,
                              BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="GLB2VRM local web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    state = conv.status()
    print("GLB2VRM  http://%s:%d" % (args.host, args.port))
    if state["blender"]:
        print("Blender  %s (%s)" % (state["blender"]["path"], state["blender"]["version"]))
    else:
        print("Blender  NOT FOUND - set GLB2VRM_BLENDER or install Blender")
    server = QuietServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
