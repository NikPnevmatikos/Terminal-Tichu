"""Browser gateway: play Tichu with nothing installed but a web browser.

The host runs this; players open http://<host>:8080 on a laptop or phone.
For every visitor the gateway spawns the real terminal client
(``tichu.client``) as a subprocess pointed at the game server, streams its
output to the page over Server-Sent Events, and feeds typed commands back to
its stdin. The game server itself is spawned too (unless one is already
listening), so hosting is one command:

    python -m tichu.web [--port 8080] [--bots N] [--target 1000] ...

Terminal players can still join the same table directly on the game port.
Pure stdlib, like everything else here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .protocol import DEFAULT_PORT

ROOT = Path(__file__).resolve().parents[1]
WEB_PORT = 8080
NAME_RE = re.compile(r"[^\w\-]", re.UNICODE)
IDLE_KILL_S = 15 * 60   # no browser attached for this long -> end the session
DEAD_KEEP_S = 5 * 60    # keep an ended session's log around this long


def _pythonpath() -> str:
    """The repo root, ahead of whatever PYTHONPATH is already set."""
    existing = os.environ.get("PYTHONPATH")
    return str(ROOT) + (os.pathsep + existing if existing else "")


# --------------------------------------------------------------------- #
# sessions: one terminal-client subprocess per browser player

class Session:
    def __init__(self, sid: str, name: str, proc: subprocess.Popen, cwd: str):
        self.sid = sid
        self.name = name
        self.proc = proc
        self.cwd = cwd
        self.lines: list[str] = []
        self.cond = threading.Condition()
        self.alive = True
        self.died_at: float | None = None
        self.listeners = 0
        self.last_seen = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            with self.cond:
                self.lines.append(text)
                self.cond.notify_all()
        self.proc.wait()
        with self.cond:
            self.alive = False
            self.died_at = time.time()
            self.cond.notify_all()

    def send(self, line: str) -> bool:
        if not self.alive or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def end(self) -> None:
        self.send("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def cleanup(self) -> None:
        shutil.rmtree(self.cwd, ignore_errors=True)


class Gateway:
    def __init__(self, game_port: int):
        self.game_port = game_port
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def join(self, name: str, spectate: bool) -> Session:
        name = NAME_RE.sub("", name.strip())[:16] or "player"
        cwd = tempfile.mkdtemp(prefix="tichu-web-")
        cmd = [sys.executable, "-m", "tichu.client",
               "--host", "127.0.0.1", "--port", str(self.game_port), "--name", name]
        if spectate:
            cmd.append("--spectate")
        env = dict(os.environ,
                   PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   TICHU_FORCE_COLOR="1", PYTHONPATH=_pythonpath())
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        sid = secrets.token_urlsafe(16)
        sess = Session(sid, name, proc, cwd)
        with self.lock:
            self.sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(sid)

    def _reaper(self) -> None:
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                items = list(self.sessions.items())
            for sid, s in items:
                if s.alive and s.listeners == 0 and now - s.last_seen > IDLE_KILL_S:
                    s.end()
                if not s.alive and s.died_at and now - s.died_at > DEAD_KEEP_S:
                    s.cleanup()
                    with self.lock:
                        self.sessions.pop(sid, None)


# --------------------------------------------------------------------- #
# the page (static; everything dynamic goes over fetch/SSE)

PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content">
<title>Terminal Tichu</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { background:#0d1117; color:#d6dce3; height:100dvh; display:flex;
         flex-direction:column; font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  header { padding:8px 12px; background:#161b22; border-bottom:1px solid #2d333b;
           display:flex; gap:10px; align-items:baseline; flex:none; }
  header b { color:#e6b450; }
  #status { font-size:12px; color:#768390; }
  #log { flex:1; overflow-y:auto; padding:10px 12px; white-space:pre-wrap; word-break:break-word; }
  #joinbox { margin:auto; display:flex; flex-direction:column; gap:12px; padding:24px;
             background:#161b22; border:1px solid #2d333b; border-radius:8px; width:min(320px,90vw); }
  #joinbox input { background:#0d1117; border:1px solid #2d333b; color:#d6dce3;
                   padding:10px; border-radius:6px; font:inherit; }
  button { background:#1f6feb; color:#fff; border:0; padding:10px 14px; border-radius:6px;
           font:inherit; cursor:pointer; }
  button.ghost { background:transparent; color:#768390; border:1px solid #2d333b; }
  #bar { flex:none; display:flex; gap:6px; padding:8px; background:#161b22;
         border-top:1px solid #2d333b; }
  #cmd { flex:1; background:#0d1117; border:1px solid #2d333b; color:#d6dce3;
         padding:10px; border-radius:6px; font:inherit; min-width:0; }
  #quick { flex:none; display:flex; gap:6px; padding:0 8px 8px; background:#161b22;
           overflow-x:auto; }
  #quick button { background:#21262d; color:#adbac7; padding:6px 10px; font-size:12px; }
  .b{font-weight:700} .dim{opacity:.55} .red{color:#f47067} .green{color:#57ab5a}
  .yellow{color:#c69026} .blue{color:#539bf5} .magenta{color:#b083f0}
  .cyan{color:#39c5cf} .white{color:#f0f3f6}
  .bg-green{background:#2ea043;color:#fff} .bg-red{background:#da3633;color:#fff}
  .hidden{display:none!important}
</style></head>
<body>
<header><b>Terminal Tichu</b><span id="status">not connected</span></header>
<div id="log"></div>
<div id="join">
  <div id="joinbox">
    <div style="font-weight:700">Join the table</div>
    <input id="name" placeholder="your name" maxlength="16" autocomplete="off">
    <button id="joinbtn">Join</button>
    <button id="specbtn" class="ghost">just watch</button>
  </div>
</div>
<div id="quick" class="hidden">
  <button data-c="pass">pass</button><button data-c="board">board</button>
  <button data-c="hand">hand</button><button data-c="tichu">tichu</button>
  <button data-c="grand">grand</button><button data-c="take">take</button>
  <button data-c="help">help</button>
</div>
<div id="bar" class="hidden">
  <input id="cmd" placeholder="p 5s 5d &nbsp;·&nbsp; pass &nbsp;·&nbsp; help"
         autocomplete="off" autocapitalize="none" spellcheck="false">
  <button id="sendbtn">send</button>
</div>
<script>
const $ = id => document.getElementById(id);
const CLS = {"0":null,"1":"b","2":"dim","31":"red","32":"green","33":"yellow",
             "34":"blue","35":"magenta","36":"cyan","97":"white",
             "41":"bg-red","42":"bg-green"};
function ansiToHtml(s){
  let out = "", open = 0;
  for (const part of s.split(/(\\x1b\\[[0-9;]*m)/)){
    const m = part.match(/^\\x1b\\[([0-9;]*)m$/);
    if (m){
      out += "</span>".repeat(open); open = 0;
      const cls = m[1].split(";").map(c=>CLS[c]||"").filter(Boolean).join(" ");
      if (cls){ out += `<span class="${cls}">`; open = 1; }
    } else if (part){
      out += part.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }
  }
  return out + "</span>".repeat(open);
}
// The client flashes a real terminal with OSC 11 (set the background) and
// OSC 111 (restore it). Neither is text: strip them and flash the page
// instead - for live lines only, never for history replayed after a reload.
const OSC = /\\x1b\\]([^\\x07\\x1b]*)(?:\\x07|\\x1b\\\\)/g;
function stripOsc(s, live){
  return s.replace(OSC, (_, body) => {
    const m = body.match(/^11;(#[0-9a-fA-F]{6})$/);
    if (m && live) flash(m[1]);
    return "";
  });
}
function flash(color){
  const b = document.body;
  b.style.transition = "none"; b.style.background = color;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    b.style.transition = "background .6s ease-out"; b.style.background = "";
  }));
}
let sid = null, es = null;
function addLine(t, live){
  t = stripOsc(t, live);
  const el = $("log"), stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  const d = document.createElement("div");
  d.innerHTML = ansiToHtml(t) || "\\u00a0";
  el.appendChild(d);
  if (stick) el.scrollTop = el.scrollHeight;
}
function setStatus(t){ $("status").textContent = t; }
function attach(fromIdx){
  es = new EventSource(`/events?s=${sid}&i=${fromIdx}`);
  es.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.end){ setStatus("session ended — reload to rejoin"); es.close(); localStorage.removeItem("tichu_sid"); return; }
    addLine(m.d, !m.h);
  };
  es.onopen = () => setStatus("connected");
  es.onerror = () => setStatus("reconnecting…");
}
async function join(spectate){
  const name = $("name").value.trim() || "player";
  const r = await fetch("/join", {method:"POST", headers:{"content-type":"application/json"},
                                  body: JSON.stringify({name, spectate, sid: localStorage.getItem("tichu_sid")})});
  const j = await r.json();
  sid = j.sid;
  localStorage.setItem("tichu_sid", sid);
  localStorage.setItem("tichu_name", name);
  $("join").classList.add("hidden");
  $("bar").classList.remove("hidden");
  $("quick").classList.remove("hidden");
  attach(j.replay ? 0 : j.idx);
  $("cmd").focus();
}
function send(line){
  line = line.trim();
  if (!line || !sid) return;
  fetch("/cmd", {method:"POST", headers:{"content-type":"application/json"},
                 body: JSON.stringify({s: sid, line})});
}
$("joinbtn").onclick = () => join(false);
$("specbtn").onclick = () => join(true);
$("name").addEventListener("keydown", e => { if (e.key === "Enter") join(false); });
$("sendbtn").onclick = () => { send($("cmd").value); $("cmd").value = ""; $("cmd").focus(); };
$("cmd").addEventListener("keydown", e => {
  if (e.key === "Enter"){ send($("cmd").value); $("cmd").value = ""; }
});
document.querySelectorAll("#quick button").forEach(b => b.onclick = () => { send(b.dataset.c); $("cmd").focus(); });
$("name").value = localStorage.getItem("tichu_name") || "";
(async () => {   // reattach after a reload if our session is still alive
  const old = localStorage.getItem("tichu_sid");
  if (!old) return;
  const r = await fetch(`/alive?s=${old}`);
  if ((await r.json()).alive){ sid = old;
    $("join").classList.add("hidden");
    $("bar").classList.remove("hidden");
    $("quick").classList.remove("hidden");
    attach(0); $("cmd").focus();
  } else localStorage.removeItem("tichu_sid");
})();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    gateway: Gateway  # set by serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers ----
    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not 0 < n <= 4096:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    # ---- routes ----
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/alive":
            q = parse_qs(url.query)
            s = self.gateway.get((q.get("s") or [""])[0])
            self._json({"alive": bool(s and s.alive)})
        elif url.path == "/events":
            self._sse(parse_qs(url.query))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        data = self._read_json()
        if url.path == "/join":
            old = self.gateway.get(str(data.get("sid") or ""))
            if old and old.alive:  # page reload — resume the same seat
                self._json({"sid": old.sid, "replay": True, "idx": 0})
                return
            sess = self.gateway.join(str(data.get("name", "")), bool(data.get("spectate")))
            self._json({"sid": sess.sid, "replay": False, "idx": 0})
        elif url.path == "/cmd":
            s = self.gateway.get(str(data.get("s") or ""))
            line = str(data.get("line") or "")[:200].replace("\n", " ").replace("\r", " ")
            if s is None:
                self._json({"error": "no such session"}, 404)
            else:
                s.last_seen = time.time()
                self._json({"ok": s.send(line)})
        else:
            self._json({"error": "not found"}, 404)

    def _sse(self, q: dict) -> None:
        s = self.gateway.get((q.get("s") or [""])[0])
        if s is None:
            self._json({"error": "no such session"}, 404)
            return
        try:
            idx = max(0, int((q.get("i") or ["0"])[0]))
        except ValueError:
            idx = 0
        last_id = self.headers.get("Last-Event-ID")
        if last_id and last_id.isdigit():  # browser auto-reconnect resumes exactly
            idx = int(last_id) + 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        s.listeners += 1
        s.last_seen = time.time()
        with s.cond:
            backlog = len(s.lines)  # replayed history: the page must not re-flash it
        try:
            while True:
                with s.cond:
                    while idx >= len(s.lines) and s.alive:
                        if not s.cond.wait(timeout=15):
                            break  # heartbeat turn
                    chunk = s.lines[idx:]
                    ended = not s.alive and idx + len(chunk) >= len(s.lines)
                for line in chunk:
                    payload = json.dumps({"d": line, "h": 1} if idx < backlog else {"d": line})
                    self.wfile.write(f"id: {idx}\ndata: {payload}\n\n".encode())
                    idx += 1
                if ended:
                    self.wfile.write(b"data: {\"end\": true}\n\n")
                    return
                if not chunk:
                    self.wfile.write(b": ping\n\n")  # keepalive
                self.wfile.flush()
        except OSError:
            pass  # browser went away; the session lives on for a reattach
        finally:
            s.listeners -= 1
            s.last_seen = time.time()


# --------------------------------------------------------------------- #
# startup

def _port_open(port: int) -> bool:
    with socket.socket() as sk:
        sk.settimeout(0.3)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def _spawn_game_server(args: argparse.Namespace) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "tichu.server", "--host", "0.0.0.0",
           "--port", str(args.game_port), "--target", str(args.target),
           "--bot-delay", str(args.bot_delay)]
    if args.bots:
        cmd += ["--bots", str(args.bots)]
    if args.seed is not None:
        cmd += ["--seed", str(args.seed)]
    env = dict(os.environ, PYTHONPATH=_pythonpath(), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, env=env)
    for _ in range(50):
        if _port_open(args.game_port):
            return proc
        if proc.poll() is not None:
            raise SystemExit("game server failed to start")
        time.sleep(0.1)
    raise SystemExit("game server did not come up")


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal Tichu web gateway")
    ap.add_argument("--port", type=int, default=WEB_PORT, help="web port players open")
    ap.add_argument("--game-port", type=int, default=DEFAULT_PORT,
                    help="TCP game port (terminal players can join it directly)")
    ap.add_argument("--target", type=int, default=1000)
    ap.add_argument("--bots", type=int, default=0)
    ap.add_argument("--bot-delay", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    game_proc = None
    if _port_open(args.game_port):
        print(f"· using the game server already on port {args.game_port}")
    else:
        game_proc = _spawn_game_server(args)
        print(f"· game server up on port {args.game_port}")

    Handler.gateway = Gateway(args.game_port)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    httpd.daemon_threads = True
    lan_ip = "?"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            lan_ip = sk.getsockname()[0]
    except OSError:
        pass
    print(f"· players open  http://{lan_ip}:{args.port}  in any browser")
    print("  (from the internet: forward the port, or tunnel it — see README)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with Handler.gateway.lock:
            sessions = list(Handler.gateway.sessions.values())
        for s in sessions:
            s.end()
            s.cleanup()
        if game_proc is not None:
            game_proc.terminate()
        print("\nbye")


if __name__ == "__main__":
    main()
