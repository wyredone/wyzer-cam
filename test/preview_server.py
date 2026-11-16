"""Serves the AI Alerts page on this computer against a fake camera.

Run:  python test/preview_server.py [port]      then open http://localhost:8765/

The page and its script are the real files; /x/json-aiwatch.cgi runs the real
backend script through `sh` with stand-ins for the camera's commands. The
rest of thingino's web interface (menu, footer, styles) is not here, so the
page appears without them.
"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_camera  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
WWW = fake_camera.FILES / "www"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}
camera = fake_camera.FakeCamera()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, body):
        url = urlsplit(self.path)
        path = "/config-aiwatch.html" if url.path == "/" else url.path
        if path == "/x/json-aiwatch.cgi":
            status, _, text = camera.request(url.query, body)
            self.reply(status, "application/json", text.encode("utf-8"))
            return
        target = (WWW / path.lstrip("/")).resolve()
        if WWW.resolve() in target.parents and target.is_file():
            self.reply(200, TYPES.get(target.suffix, "application/octet-stream"), target.read_bytes())
        elif path.startswith("/a/"):
            # thingino's own scripts and styles: empty stand-ins
            self.reply(200, TYPES.get(Path(path).suffix, "text/plain"), b"")
        else:
            self.reply(404, "text/plain", b"not found")

    def do_GET(self):
        self.handle_any(None)

    def do_POST(self):
        self.handle_any(self.rfile.read(int(self.headers.get("Content-Length") or 0)))


if __name__ == "__main__":
    if not fake_camera.SH:
        sys.exit("sh was not found; install Git for Windows or run this from Git Bash")
    print("AI Alerts page preview: http://localhost:%d/" % PORT, flush=True)
    try:
        HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    finally:
        camera.cleanup()
