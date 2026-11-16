"""Find cameras running thingino on the local network.

A host counts as a candidate when something answers on the SSH port. It is
marked as thingino when its web page (thingino's login page) says so, which
needs no password.
"""

import concurrent.futures
import ipaddress
import re
import socket
import urllib.request


def local_network():
    """The /24 network this computer is on, or None when it cannot tell."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the interface
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if address.startswith("127."):
        return None
    return ipaddress.ip_network(address + "/24", strict=False)


def web_is_thingino(host, port=80, timeout=1.5):
    """True when the host's login page is titled like thingino's."""
    try:
        with urllib.request.urlopen("http://%s:%d/login.html" % (host, port), timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
    except (OSError, ValueError):
        return False
    match = re.search(r"<title>([^<]*)</title>", body, re.I)
    return bool(match and "thingino" in match.group(1).lower())


def probe(host, port=22, web_port=80, timeout=0.6):
    """None when nothing answers on the SSH port, else what was found."""
    try:
        sock = socket.create_connection((host, port), timeout)
    except OSError:
        return None
    banner = ""
    try:
        sock.settimeout(timeout)
        banner = sock.recv(128).decode("ascii", "replace").split("\n")[0].strip()
    except OSError:
        pass
    finally:
        sock.close()
    return {"host": host, "banner": banner, "thingino": web_is_thingino(host, web_port)}


def scan(hosts, port=22, web_port=80, progress=None, stop=None, workers=64):
    """Probes every host. `progress(done, total)` is called as results arrive and
    `stop` (a threading.Event) cancels the rest. Returns the finds, thingino first."""
    hosts = list(hosts)
    found = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe, h, port, web_port) for h in hosts]
        for future in concurrent.futures.as_completed(futures):
            if stop is not None and stop.is_set():
                for pending in futures:
                    pending.cancel()
                break
            done += 1
            result = future.result()
            if result:
                found.append(result)
            if progress:
                progress(done, len(hosts))
    found.sort(key=lambda r: (not r["thingino"], tuple(int(p) for p in r["host"].split(".")) if r["host"].replace(".", "").isdigit() else (999,)))
    return found


def scan_local(progress=None, stop=None):
    """Scans this computer's /24 network. Returns (finds, network or None)."""
    network = local_network()
    if network is None:
        return [], None
    return scan((str(h) for h in network.hosts()), progress=progress, stop=stop), network
