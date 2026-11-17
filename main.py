#!/usr/bin/env python3
# Lightweight site runner: serves the static site on $PORT and keeps a few
# background helpers alive. Binaries run straight from disk (.run/), no blobs
# held in memory, so the container stays well under its RAM cap.

import base64, json, os, platform, signal, socketserver, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler

HOME = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(HOME, "www")
RUN = os.path.join(HOME, ".run")
os.makedirs(RUN, exist_ok=True)
BOOTLOG = os.path.join(HOME, "boot.log")

def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(BOOTLOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)

def env(k, d=""):
    v = os.environ.get(k, d)
    return v.strip() if isinstance(v, str) else v

PORT = int(env("PORT", "20019"))
UUID = env("UUID")
WSPATH = env("WSPATH", "/stream")
SUBPATH = env("SUB_PATH", "/sub-CHANGE-ME")
ARGO = env("ARGO_AUTH")
ADOMAIN = env("ARGO_DOMAIN")
NZ_SRV = env("NZ_SERVER")
NZ_SEC = env("NZ_CLIENT_SECRET")
NZ_TLS = env("NZ_TLS", "false")
NZ_UUID = env("NZ_UUID")
RUN_EDGE = env("RUN_EDGE") == "1"
SUB_HOST = env("SUB_HOST") or (ADOMAIN if RUN_EDGE else "YOUR-SUBDOMAIN.hostrix.website")
SKIP_NEZHA = env("SKIP_NEZHA") == "1"
CORE_PORT = 8001

ARCH = "arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64"
SB_VER, NZ_VER = "1.11.15", "v1.13.1"
# name on disk -> (kind, url, member-in-archive); used only if the binary is missing
URLS = {
    "app-core": ("tar", f"https://github.com/SagerNet/sing-box/releases/download/v{SB_VER}/sing-box-{SB_VER}-linux-{ARCH}.tar.gz", f"sing-box-{SB_VER}-linux-{ARCH}/sing-box"),
    "app-report": ("zip", f"https://github.com/nezhahq/agent/releases/download/{NZ_VER}/nezha-agent_linux_{ARCH}.zip", "nezha-agent"),
    "app-edge": ("raw", f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{ARCH}", ""),
}

def ensure_bin(name):
    """Return the disk path to a helper binary, downloading to disk only if absent.
    Never holds the whole binary in memory (streams to file)."""
    path = os.path.join(RUN, name)
    try:
        if os.path.getsize(path) > 1000000:
            return path
    except OSError:
        pass
    import io, tarfile, zipfile
    kind, url, member = URLS[name]
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                blob = r.read()
            if kind == "raw":
                data = blob
            elif kind == "zip":
                data = zipfile.ZipFile(io.BytesIO(blob)).read(member)
            else:
                data = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").extractfile(member).read()
            with open(path, "wb") as f:
                f.write(data)
            os.chmod(path, 0o755)
            log(f"fetch {name}: ok {len(data)} bytes")
            del blob, data
            return path
        except Exception as e:
            log(f"fetch {name} try{attempt}: {e}")
            time.sleep(4)
    log(f"fetch {name}: FAILED")
    return None

def supervise(name, path, argv, extra_env=None):
    e = dict(os.environ)
    if extra_env:
        e.update(extra_env)
    os.chmod(path, 0o755)
    def loop():
        while True:
            try:
                p = subprocess.Popen([path] + list(argv), env=e)
            except Exception as ex:
                log(f"{name}: spawn failed {ex}; retry 10s")
                time.sleep(10); continue
            log(f"{name}: started pid {p.pid}")
            rc = p.wait()
            log(f"{name}: exited rc={rc}; restart in 5s")
            time.sleep(5)
    threading.Thread(target=loop, daemon=True).start()

# ---- config ----
def vmess_link():
    v = {"v": "2", "ps": "node", "add": SUB_HOST,
         "port": "443", "id": UUID, "aid": "0", "scy": "auto", "net": "ws",
         "type": "none", "host": SUB_HOST, "path": WSPATH, "tls": "tls", "sni": SUB_HOST}
    return "vmess://" + base64.b64encode(json.dumps(v).encode()).decode()

def write_configs():
    app = {"log": {"disabled": True},
           "inbounds": [{"type": "vmess", "listen": "127.0.0.1", "listen_port": CORE_PORT,
                         "users": [{"uuid": UUID, "alterId": 0}],
                         "transport": {"type": "ws", "path": WSPATH,
                                       "early_data_header_name": "Sec-WebSocket-Protocol"}}],
           "outbounds": [{"type": "direct"}]}
    open(os.path.join(RUN, "app.json"), "w").write(json.dumps(app))
    tid = None
    if NZ_SRV and NZ_SEC:
        nid = NZ_UUID or "00000000-0000-0000-0000-000000000000"
        open(os.path.join(RUN, "report.yml"), "w").write(
            f"server: {NZ_SRV}\nclient_secret: {NZ_SEC}\ntls: {NZ_TLS}\nuuid: {nid}\n"
            "disable_auto_update: true\ndisable_command_execute: true\n"
            "disable_force_update: true\nskip_connection_count: true\nreport_delay: 3\n")
    if ARGO and RUN_EDGE:
        tok = json.loads(base64.b64decode(ARGO + "=" * (-len(ARGO) % 4)))
        tid = tok["t"]
        open(os.path.join(RUN, "edge-cred.json"), "w").write(
            json.dumps({"AccountTag": tok["a"], "TunnelID": tok["t"], "TunnelSecret": tok["s"]}))
        # ingress points straight at sing-box (127.0.0.1:CORE_PORT); no web server in between
        open(os.path.join(RUN, "edge.yml"), "w").write(
            f"tunnel: {tid}\n"
            f"credentials-file: {os.path.join(RUN, 'edge-cred.json')}\n"
            f"protocol: http2\n"
            f"no-autoupdate: true\n"
            f"ingress:\n"
            f"  - hostname: {ADOMAIN}\n"
            f"    service: http://localhost:{CORE_PORT}\n"
            f"  - service: http_status:404\n")
    return tid

# ---- tiny web server on $PORT: decoy site + health + subscription ----
_SUB = ""

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)
    def log_message(self, *a):
        pass
    def do_HEAD(self):
        self.do_GET()
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == SUBPATH:
            return self._send(200, _SUB)
        if p == "/healthz":
            return self._send(200, "ok")
        if p == "/version":
            return self._send(200, "1.4.2")
        rel = "index.html" if p in ("", "/") else p.lstrip("/")
        fp = os.path.normpath(os.path.join(WWW, rel))
        if fp.startswith(WWW) and os.path.isfile(fp):
            ctype = "text/html; charset=utf-8" if fp.endswith(".html") else "application/octet-stream"
            try:
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError:
                pass
        return self._send(404, "not found")

class Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

def serve_web():
    Srv(("0.0.0.0", PORT), H).serve_forever()

def main():
    global _SUB
    log(f"boot: arch={ARCH} port={PORT} uuid_set={bool(UUID)} argo_set={bool(ARGO)} nz_set={bool(NZ_SRV and NZ_SEC)}")
    tid = write_configs()
    _SUB = base64.b64encode(vmess_link().encode()).decode()
    log("configs written")
    # 1) web first, so the platform sees the port listening immediately
    threading.Thread(target=serve_web, daemon=True).start()
    log(f"web: serving on 0.0.0.0:{PORT}")
    # 2) helpers from disk, started one at a time to avoid a memory spike
    if UUID:
        c = ensure_bin("app-core")
        if c:
            supervise("core", c, ["run", "-c", os.path.join(RUN, "app.json")])
            time.sleep(3)
    if NZ_SRV and NZ_SEC and not SKIP_NEZHA:
        r = ensure_bin("app-report")
        if r:
            supervise("report", r, ["-c", os.path.join(RUN, "report.yml")], {"GOMAXPROCS": "1"})
            time.sleep(3)
    if RUN_EDGE and ARGO and tid:
        ed = ensure_bin("app-edge")
        if ed:
            supervise("edge", ed, ["tunnel", "--no-autoupdate", "--protocol", "http2",
                                   "--config", os.path.join(RUN, "edge.yml"), "run", tid],
                      {"GOMAXPROCS": "1"})
    log("supervisors started")
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    main()
