"""Mock OpenAI-compatible endpoint for the aiwatch tests.

Usage: mock_server.py PORT STATE_DIR

STATE_DIR/mode picks the reply. It is either one word, used for every model,
or a JSON object mapping model ids to words ("*" is the default):
  {"*": "notify", "free/a:free": "429"}
Every request is logged: its body in request.json, its Authorization header in
auth, and one line per request with the model id in hits.log.
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
    "tricky": '{"description": "Sign says \\"50% off\\" \\\\ sale it\'s `id` $(id)", "notify": true, "rule": ""}',
    "prose": "I can see a driveway.",
    "empty": "",
}
ERRORS = {"error": 500, "429": 429, "401": 401, "402": 402, "404": 404}


def mode_for(model):
    with open(os.path.join(STATE, "mode")) as f:
        text = f.read().strip()
    if text.startswith("{"):
        modes = json.loads(text)
        return modes.get(model, modes.get("*", "notify"))
    return text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(os.path.join(STATE, "request.json"), "wb") as f:
            f.write(body)
        with open(os.path.join(STATE, "auth"), "w") as f:
            f.write(self.headers.get("Authorization", ""))
        model = json.loads(body).get("model", "")
        with open(os.path.join(STATE, "hits"), "a") as f:
            f.write("x")
        with open(os.path.join(STATE, "hits.log"), "a", newline="\n") as f:  # no CRLF on Windows
            f.write(model + "\n")

        mode = mode_for(model)
        if mode in ERRORS:
            code = ERRORS[mode]
            message = "upstream exploded" if mode == "error" else "mock %s" % mode
            reply = {"error": {"message": message, "code": code}}
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
