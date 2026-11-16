"""Mock OpenAI-compatible endpoint for the aiwatch tests.

Usage: mock_server.py PORT STATE_DIR

Reads the reply to give from STATE_DIR/mode and stores each request body in
STATE_DIR/request.json and its Authorization header in STATE_DIR/auth.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
STATE = sys.argv[2]

ANSWERS = {
    "notify": '{"description": "A courier leaves a parcel at the door.", "notify": true, "rule": "A person is at the door"}',
    "quiet": '{"description": "An empty driveway at dusk.", "notify": false, "rule": ""}',
    "fenced": '```json\n{"description": "A dog runs across the lawn.",\n "notify": true, "rule": ""}\n```',
    "tricky": '{"description": "Sign says \\"50% off\\" \\\\ sale", "notify": true, "rule": ""}',
    "prose": "I can see a driveway.",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(os.path.join(STATE, "request.json"), "wb") as f:
            f.write(body)
        with open(os.path.join(STATE, "auth"), "w") as f:
            f.write(self.headers.get("Authorization", ""))
        with open(os.path.join(STATE, "hits"), "a") as f:
            f.write("x")
        with open(os.path.join(STATE, "mode")) as f:
            mode = f.read().strip()

        if mode == "error":
            code, reply = 500, {"error": {"message": "upstream exploded"}}
        else:
            code = 200
            reply = {"choices": [{"message": {"role": "assistant", "content": ANSWERS[mode]}}]}
        data = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
