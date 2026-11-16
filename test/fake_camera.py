"""A fake camera environment for running the web backend off the camera.

Creates a temp directory with stand-ins for the camera's commands and files,
and runs package/.../www/x/json-aiwatch.cgi in it the way httpd would.
Used by test_webui.py and preview_server.py.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = HERE.parent / "package" / "thingino-aiwatch" / "files"
CGI = FILES / "www" / "x" / "json-aiwatch.cgi"


def find_sh():
    """`sh` from PATH, or Git for Windows' copy when PATH does not have one."""
    found = shutil.which("sh")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        candidate = base and os.path.join(base, "Git", "bin", "sh.exe")
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


SH = find_sh()


class FakeCamera:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="wyzer-cam-"))
        self.bin = self.dir / "bin"
        self.state = self.dir / "state"
        self.config = self.dir / "aiwatch.json"
        self.motion = self.dir / "motion"
        self.calls = self.dir / "calls"
        self.authorized = True
        for d in (self.bin, self.state, self.dir / "x"):
            d.mkdir()
        shutil.copy(FILES / "aiwatch.json", self.config)
        self.motion.write_text("#!/bin/sh\n[ -x /usr/sbin/aiwatch ] && aiwatch motion &\n")
        (self.dir / "prudynt.json").write_text('{"motion": {"enabled": false}}')
        # auth.sh: the real one checks the session; this one obeys a flag file.
        (self.dir / "x" / "auth.sh").write_bytes(
            b"require_auth() {\n"
            b"\tif [ -f \"$FAKE_DENY\" ]; then\n"
            b"\t\tprintf 'Status: 401 Unauthorized\\r\\n\\r\\n'\n"
            b"\t\texit 0\n"
            b"\tfi\n"
            b"}\n")
        calls = self.calls.as_posix()
        self.stub("jct", 'exec "%s" -I "%s" "$@"' % (Path(sys.executable).as_posix(),
                                                      (HERE / "mock_jct.py").as_posix()))
        self.stub("prudyntctl", "cat >/dev/null; echo prudyntctl \"$@\" >>'%s'" % calls)
        self.stub("aiwatch",
                  "echo aiwatch \"$@\" >>'%s'\n"
                  "[ -f '%s' ] && { echo 'aiwatch: request failed: HTTP 401 \"bad\" key'; exit 1; }\n"
                  "echo 'description: A courier leaves a parcel at the door.'\n"
                  "echo 'notify: true'\n"
                  "echo 'rule: A person is at the door'"
                  % (calls, (self.dir / "aiwatch_fails").as_posix()))

    def stub(self, name, body):
        path = self.bin / name
        path.write_bytes(("#!/bin/sh\n%s\n" % body).encode())
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def called(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def request(self, query="", body=None):
        """Runs the CGI. Returns (status code, parsed JSON or None, raw body)."""
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env.update(
            AIWATCH_WWW_X=(self.dir / "x").as_posix(),
            AIWATCH_CONFIG=self.config.as_posix(),
            AIWATCH_STATE_DIR=self.state.as_posix(),
            AIWATCH_MOTION=self.motion.as_posix(),
            AIWATCH_PRUDYNT_JSON=(self.dir / "prudynt.json").as_posix(),
            FAKE_DENY=(self.dir / "deny").as_posix(),
            QUERY_STRING=query,
            REQUEST_METHOD="POST" if body is not None else "GET",
            CONTENT_LENGTH=str(len(body)) if body is not None else "",
        )
        result = subprocess.run([SH, CGI.as_posix()], input=body or b"", env=env,
                                capture_output=True, timeout=120)
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        status = 200
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"status:"):
                status = int(line.split()[1])
        text = payload.decode("utf-8", "replace")
        try:
            return status, json.loads(text), text
        except ValueError:
            return status, None, text
