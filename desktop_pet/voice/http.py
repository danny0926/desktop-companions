"""Optional stdlib loopback HTTP wrapper for the same VoiceService instance."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .service import VoiceService


def serve(service: VoiceService, *, host: str = "127.0.0.1", port: int = 8767) -> None:
    if host != "127.0.0.1":
        raise ValueError("The desktop voice API binds to 127.0.0.1 only.")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_json(self, value: dict, code=200):
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError("JSON request body must be between 1 and 16384 bytes.")
            value = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object.")
            return value

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    self.send_json(service.health())
                elif path.startswith("/requests/"):
                    self.send_json(service.get_request(path.removeprefix("/requests/")))
                elif path.startswith("/audio/"):
                    result = service.get_request(path.removeprefix("/audio/"))
                    if not result.get("playable") or not result.get("watermark_complete"):
                        self.send_json({"error": "Audio is cancelled, stale, or not ready."}, 409)
                        return
                    wav = Path(result["wav_path"])
                    payload = wav.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_json({"error": "Unknown route."}, 404)
            except KeyError:
                self.send_json({"error": "Unknown request_id."}, 404)
            except (ValueError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                value = self.body()
                if path == "/synthesize":
                    self.send_json(service.synthesize(value, timeout=float(service.config["request_timeout_seconds"])))
                elif path == "/submit":
                    request_id = service.submit(value)
                    self.send_json(service.get_request(request_id), 202)
                elif path == "/turn":
                    self.send_json(service.begin_turn(value["turn_id"]))
                elif path == "/cancel":
                    self.send_json(service.cancel(turn_id=value.get("turn_id"), request_id=value.get("request_id")))
                elif path == "/prewarm":
                    request_id = service.submit_prewarm()
                    self.send_json(service.get_request(request_id), 202)
                elif path == "/profile":
                    self.send_json(service.set_profile(value["profile"]))
                else:
                    self.send_json({"error": "Unknown route."}, 404)
            except KeyError as exc:
                self.send_json({"error": f"Missing field: {exc}"}, 400)
            except (TypeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, 400)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        service.close()
