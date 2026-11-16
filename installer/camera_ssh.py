"""Talk to a camera that runs thingino: inspect it, install, update or remove
the aiwatch addon, run a test, restart it, and set up key login.

Two executors run commands on the camera:
  SshExecutor    the real one, over SSH with paramiko. It only talks to a host
                 whose key was shown to the user and accepted.
  LocalExecutor  runs the same commands in a local `sh` against a fake camera
                 directory, for the tests.

The camera's dropbear has no SFTP, so files are sent as `cat > path` with the
content on the command's standard input.
"""

import base64
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import wyzer_core as core

HERE = Path(__file__).resolve().parent
REMOTE_DIR = "/tmp/wyzer-card"
# Commands run on the camera get the sbin directories added to PATH: a
# non-interactive SSH command may not have them.
PATH_FIX = 'PATH="$PATH:/usr/sbin:/sbin"; export PATH; '
MIN_FREE_KB = 300


class CameraError(Exception):
    """Something went wrong talking to the camera; the message is for the user."""


def _paramiko():
    """The paramiko module, or a CameraError that says how to get it."""
    try:
        import paramiko
    except ImportError:
        raise CameraError("The SSH part needs the 'paramiko' package. Install it with:  pip install paramiko")
    return paramiko


# --- host keys -----------------------------------------------------------------

def fingerprint(key):
    """OpenSSH-style SHA256 fingerprint of a paramiko key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def fetch_host_key(host, port=22, timeout=10):
    """The key the camera presents, before logging in. Returns (key, fingerprint)."""
    paramiko = _paramiko()

    try:
        sock = socket.create_connection((host, port), timeout)
    except OSError as exc:
        raise CameraError("Could not reach %s on port %d: %s" % (host, port, exc))
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
    except (paramiko.SSHException, EOFError, OSError) as exc:
        raise CameraError("%s did not answer like an SSH server: %s" % (host, exc))
    finally:
        transport.close()
    return key, fingerprint(key)


class KnownHosts:
    """Fingerprints of cameras the user has accepted, kept in the app folder."""

    def __init__(self, path=None):
        self.path = Path(path) if path else core.app_dir() / "known_hosts.json"

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def check(self, host, fp):
        """"known", "new" (never seen) or "changed" (a different key than before)."""
        stored = self._load().get(host)
        if stored is None:
            return "new"
        return "known" if stored == fp else "changed"

    def remember(self, host, fp):
        data = self._load()
        data[host] = fp
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def forget(self, host):
        data = self._load()
        if data.pop(host, None) is not None:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- keys for passwordless login -------------------------------------------------

def ssh_dir():
    return Path(os.environ.get("WYZER_SSH_DIR") or Path.home() / ".ssh")


def find_key_files():
    """The user's existing private keys, best first."""
    return [p for p in (ssh_dir() / n for n in ("id_ed25519", "id_ecdsa", "id_rsa")) if p.is_file()]


def load_key(path):
    """A paramiko key from a file, or None if it needs a passphrase or is unreadable."""
    paramiko = _paramiko()

    try:
        return paramiko.PKey.from_path(str(path))
    except AttributeError:  # older paramiko: try each key type
        for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return key_class.from_private_key_file(str(path))
            except (paramiko.SSHException, OSError, ValueError):
                continue
        return None
    except (paramiko.SSHException, OSError, ValueError):
        return None


def public_line(key, comment="wyzer-cam"):
    return "%s %s %s" % (key.get_name(), key.get_base64(), comment)


def create_key():
    """Makes ~/.ssh/id_ed25519 (the standard place) when the user has no key. Returns its path."""
    folder = ssh_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "id_ed25519"
    if target.exists():
        raise CameraError("%s already exists" % target)
    keygen = shutil.which("ssh-keygen")
    if keygen:
        result = subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "wyzer-cam", "-f", str(target)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and target.exists():
            return target
    paramiko = _paramiko()

    target = folder / "id_ecdsa"
    if target.exists():
        raise CameraError("%s already exists" % target)
    paramiko.ECDSAKey.generate(bits=256).write_private_key_file(str(target))
    return target


# --- executors ---------------------------------------------------------------------

class SshExecutor:
    """Runs commands on a camera over SSH. Use connect() first."""

    def __init__(self, host, port=22, user="root"):
        self.host, self.port, self.user = host, port, user
        self.client = None

    def connect(self, host_key, password=None, key_files=(), timeout=15):
        """Logs in, trying keys first and then the password. `host_key` must be
        the key the user accepted: a camera presenting any other is refused."""
        paramiko = _paramiko()

        attempts = [("key", load_key(p)) for p in key_files]
        attempts = [("key", k) for _, k in attempts if k is not None]
        if password:
            attempts.append(("password", password))
        if not attempts:
            raise CameraError("Enter the camera's password, or set up key login.")
        last = None
        for kind, secret in attempts:
            client = paramiko.SSHClient()
            name = self.host if self.port == 22 else "[%s]:%d" % (self.host, self.port)
            client.get_host_keys().add(name, host_key.get_name(), host_key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                client.connect(self.host, self.port, self.user, allow_agent=False, look_for_keys=False,
                               timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                               password=secret if kind == "password" else None,
                               pkey=secret if kind == "key" else None)
            except paramiko.BadHostKeyException:
                client.close()
                raise CameraError("The camera's identity changed while connecting. Nothing was sent.")
            except paramiko.AuthenticationException as exc:
                client.close()
                last = exc
                continue
            except (paramiko.SSHException, OSError, EOFError) as exc:
                client.close()
                raise CameraError("Could not log in to %s: %s" % (self.host, exc))
            self.client = client
            self.login = kind
            return kind
        raise CameraError("The camera rejected the login. Check the password for user '%s'." % self.user)

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def run(self, command, stdin=None, timeout=120):
        """Returns (exit code, stdout, stderr) as text."""
        if not self.client:
            raise CameraError("Not connected.")
        channel = self.client.get_transport().open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.exec_command(PATH_FIX + command)
        if stdin:
            channel.sendall(stdin)
        channel.shutdown_write()
        out, err = [], []
        deadline = time.time() + timeout
        while True:
            progressed = False
            while channel.recv_ready():
                out.append(channel.recv(65536))
                progressed = True
            while channel.recv_stderr_ready():
                err.append(channel.recv_stderr(65536))
                progressed = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.time() > deadline:
                channel.close()
                raise CameraError("The camera did not finish in %d seconds." % timeout)
            if not progressed:
                time.sleep(0.02)
        code = channel.recv_exit_status()
        channel.close()
        return code, b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace")


class LocalExecutor:
    """Runs commands in a local `sh`, for tests. `env` is added to the environment."""

    def __init__(self, env=None, sh=None):
        self.env = dict(os.environ)
        self.env.update(env or {})
        self.sh = sh or shutil.which("sh")

    def run(self, command, stdin=None, timeout=120):
        result = subprocess.run([self.sh, "-c", PATH_FIX + command], input=stdin or b"", env=self.env,
                                capture_output=True, timeout=timeout)
        return (result.returncode, result.stdout.decode("utf-8", "replace"),
                result.stderr.decode("utf-8", "replace"))


# --- what we do on the camera --------------------------------------------------------

def _script(name):
    return (HERE / name).read_bytes().replace(b"\r\n", b"\n")


def inspect(ex):
    """Facts about the camera, as {key: value}."""
    code, out, err = ex.run("sh -s", stdin=_script("inspect.sh"))
    if code != 0:
        raise CameraError("Could not read the camera's details: %s" % (err.strip() or "exit code %d" % code))
    info = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key.strip()] = value.strip()
    return info


def check_ready(info):
    """(errors, warnings) about whether the addon can be installed on this camera."""
    errors, warnings = [], []
    if info.get("thingino") != "yes":
        errors.append("This does not look like a camera running thingino. The SSH installer only adds to "
                      "thingino; use the SD card tab to install it first.")
    elif info.get("motion_script") != "yes" or info.get("prudynt") != "yes":
        errors.append("This thingino build does not use the prudynt streamer (no motion script found). "
                      "aiwatch only supports the stable branch.")
    for tool in ("jct", "curl"):
        if info.get(tool) != "yes":
            errors.append("The camera has no '%s', which aiwatch needs." % tool)
    free = info.get("free_kb", "")
    if free.isdigit() and int(free) < MIN_FREE_KB:
        errors.append("The camera has only %s KB of free storage; aiwatch needs about %d KB." % (free, MIN_FREE_KB))
    if info.get("webui") != "yes":
        warnings.append("This camera has no web interface, so the AI Alerts page will not be installed.")
    return errors, warnings


def read_existing_config(ex):
    """The camera's current /etc/aiwatch.json, or None if there is none."""
    # WYZER_ROOT is only set when the tests run against a fake camera directory.
    code, out, _ = ex.run('cat "${WYZER_ROOT:-}/etc/aiwatch.json"')
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def settings_from_config(config, base=None):
    """Form settings filled from a camera's aiwatch.json. The API key is left blank
    on purpose: a blank key on update means "keep the one on the camera"."""
    s = dict(base or core.DEFAULTS)
    s["api_key"] = ""
    for key in ("endpoint", "model", "camera_name", "context", "language", "extra_instructions",
                "active_from", "active_to"):
        if isinstance(config.get(key), str):
            s[key] = config[key]
    for key in ("enabled", "free_pool"):
        if isinstance(config.get(key), bool):
            s[key] = config[key]
    for key in ("cooldown", "daily_limit", "free_daily_limit", "max_attempts"):
        if isinstance(config.get(key), int) and not isinstance(config.get(key), bool):
            s[key] = str(config[key])
    if isinstance(config.get("rules"), list):
        s["rules"] = [r for r in config["rules"] if isinstance(r, str)]
    if isinstance(config.get("free_models"), list):
        s["free_models"] = [m for m in config["free_models"] if isinstance(m, str)]
    return s


@dataclass
class InstallResult:
    log: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    restart_needed: bool = False

    @property
    def ok(self):
        return not self.errors


def _put(ex, path, data):
    code, _, err = ex.run("umask 077; cat > %s" % shlex.quote(path), stdin=data)
    if code != 0:
        raise CameraError("Could not write %s on the camera: %s" % (path, err.strip() or "exit code %d" % code))


def install(ex, settings, progress=None, remote_dir=REMOTE_DIR):
    """Sends the files to the camera and runs runonce.sh there. Everything the
    camera received is deleted afterwards, secrets included."""
    say = progress or (lambda text: None)
    files = core.card_files(settings)
    quoted = shlex.quote(remote_dir)
    try:
        say("Preparing a private folder on the camera")
        code, _, err = ex.run("rm -rf %s && umask 077 && mkdir -p %s/wyzer/www/a/plugins %s/wyzer/www/x"
                              % (quoted, quoted, quoted))
        if code != 0:
            raise CameraError("Could not make a folder on the camera: %s" % err.strip())
        for number, (name, data) in enumerate(sorted(files.items()), 1):
            say("Sending %s (%d of %d)" % (name, number, len(files)))
            _put(ex, "%s/%s" % (remote_dir, name), data)
        say("Running the setup on the camera")
        code, out, err = ex.run("WYZER_NO_REBOOT=1 sh %s/runonce.sh" % quoted, timeout=180)
        _, log_text, _ = ex.run("cat %s/wyzer/install.log" % quoted)
    finally:
        ex.run("rm -rf %s" % quoted)

    result = InstallResult()
    for line in log_text.splitlines():
        text = line[20:] if len(line) > 20 and line[4] == "-" else line  # drop the timestamp
        result.log.append(text)
        if text.startswith("ERROR"):
            result.errors.append(text)
        if "restart needed" in text:
            result.restart_needed = True
    if code != 0 and not result.errors:
        result.errors.append("The setup script stopped with exit code %d: %s" % (code, (err or out).strip()))
    return result


def uninstall(ex, keep_config=False):
    """Removes the addon. Returns the list of result lines ("ok: ..." or "ERROR: ...")."""
    command = "sh -s -- --keep-config" if keep_config else "sh -s"
    code, out, err = ex.run(command, stdin=_script("uninstall.sh"))
    lines = [line for line in out.splitlines() if line.strip()]
    if code != 0:
        lines.append("ERROR: the removal stopped with exit code %d %s" % (code, err.strip()))
    return lines


def run_test(ex, send=False):
    """Runs `aiwatch test` on the camera. Returns (ok, output text)."""
    code, out, err = ex.run("aiwatch test -n" if send else "aiwatch test", timeout=120)
    return code == 0, (out + err).strip()


def restart(ex):
    """Restarts the camera. The connection ends, which is expected."""
    try:
        ex.run("sync; reboot", timeout=10)
    except (CameraError, OSError):
        pass


def authorize_key(ex, line):
    """Adds a public key to root's authorized keys, once. Returns True if it was added."""
    code, out, _ = ex.run('cat "${HOME:-/root}/.ssh/authorized_keys" 2>/dev/null')
    key_part = line.split()[1]
    if code == 0 and any(key_part in existing.split() for existing in out.splitlines()):
        return False
    code, _, err = ex.run('umask 077; mkdir -p "${HOME:-/root}/.ssh" && cat >> "${HOME:-/root}/.ssh/authorized_keys"',
                          stdin=(line.strip() + "\n").encode())
    if code != 0:
        raise CameraError("Could not save the key on the camera: %s" % err.strip())
    return True
