"""The switch on the Mac, its page, and the command line behind both.

The switch lives **on the laptop**, and that is the design rather than a
convenience: the machine that can actually stop answering is the machine that
holds the state, so there is one source of truth instead of two that disagree
when the network is the thing that is broken. The Space learns the state from
every poll, which is how it can say *switched off* instead of *offline*.

Two ways to flip it, because two ways is the difference between "usable" and
"usable while annoyed":

* **A page** at ``http://127.0.0.1:8787`` — one big button, and a status line
  that answers the questions you actually have (*is Ollama up, is the model
  pulled, is the Space reachable, how long did the last answer take*). Bound to
  loopback only: nothing on the network can reach it, and it holds no credential.
* **``decat on`` / ``decat off``** in a terminal, for when the page is not the
  fastest thing to hand.

``off`` does not stop the agent polling. It keeps polling and says "off", on
purpose — a machine that goes silent *because it was told to* is indistinguishable
from one that is asleep, and the whole point of the switch is that a person gets
a different message.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from decat import config
from decat.mac import agent as agent_module

#: Loopback only. A switch on somebody's LAN is a switch somebody else can flip.
PAGE_HOST = "127.0.0.1"
PAGE_PORT = 8787

_HELP = """decat — ask this machine questions from Discord.

  decat on           start answering
  decat off          stop answering (the bot says so, rather than saying offline)
  decat status       what the switch says, and what the last few questions did
  decat doctor       check this machine end to end: Ollama, model, web, host
  decat run          answer questions in the foreground (Ctrl-C to stop)
  decat serve        only the switch page, without answering anything

The page is at http://127.0.0.1:8787 — loopback, so nothing on the network can
reach it. `decat run` is what the login item runs for you in the background.
"""


def state_path() -> Path:
    return config.home() / "state.json"


def load_state(path: Path | None = None) -> dict:
    """The saved state, or the default: **on**."""
    target = Path(path) if path else state_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"on": bool(data.get("on", True))}
    except (OSError, ValueError):
        pass
    return {"on": True}


def save_state(state: dict, path: Path | None = None) -> Path:
    target = Path(path) if path else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"on": bool(state.get("on", True))}) + "\n", encoding="utf-8")
    return target


class Switch(agent_module.Switch):
    """The state, in a file, read fresh on every poll.

    Read on every poll rather than cached because the page and the CLI are
    different processes from the running agent: a cached value would mean flipping
    the switch and waiting for a restart, which is not a switch.
    """

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else state_path()
        super().__init__(on=load_state(self._path)["on"])

    def refresh(self) -> bool:
        self.on = bool(load_state(self._path)["on"])
        return self.on

    def set(self, value: bool) -> bool:
        save_state({"on": bool(value)}, self._path)
        self.on = bool(value)
        return self.on


def _page(controller: "Controller") -> bytes:
    status = controller.status()
    colour = "#2ecc71" if status["on"] else "#e74c3c"
    word = "ON" if status["on"] else "OFF"
    facts = "\n".join(
        f"<li><b>{name}</b> — {value}</li>" for name, value in status["facts"].items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decat</title>
<style>
 body {{ margin:0; font:15px/1.5 -apple-system,system-ui,sans-serif;
        background:#11131a; color:#e8eaf1; display:grid; place-items:center; min-height:100vh }}
 main {{ width:min(640px,92vw) }}
 h1 {{ font-size:19px; margin:0 0 14px; letter-spacing:.4px }}
 button {{ width:100%; padding:22px; font-size:26px; font-weight:700; letter-spacing:2px;
           border:0; border-radius:16px; color:#0b0d12; background:{colour}; cursor:pointer }}
 button:hover {{ filter:brightness(1.08) }}
 ul {{ margin:18px 0 0; padding:14px 18px; list-style:none; background:#181b24; border-radius:14px }}
 li {{ padding:3px 0; color:#b9bfd0 }}
 li b {{ color:#e8eaf1 }}
 p {{ color:#7f879c; font-size:13px; margin-top:16px }}
</style></head>
<body><main>
 <h1>🛰️ Decat — this Mac</h1>
 <button id="sw">{word}</button>
 <ul>{facts}</ul>
 <p>Loopback only. Nothing on the network can reach this page, and it holds no
 credential. While this says OFF the bot answers “the Mac's switched off” instead
 of “offline”.</p>
</main>
<script>
 const sw = document.getElementById('sw');
 async function paint() {{
   const r = await fetch('/api/state'); const s = await r.json();
   sw.textContent = s.on ? 'ON' : 'OFF';
   sw.style.background = s.on ? '#2ecc71' : '#e74c3c';
 }}
 sw.addEventListener('click', async () => {{
   await fetch('/toggle', {{ method:'POST' }}); await paint(); location.reload();
 }});
 paint();
</script></body></html>""".encode("utf-8")


class Controller:
    """The state, the page's facts, and the agent — if one is running."""

    def __init__(self, *, path: Path | None = None, agent: agent_module.Agent | None = None):
        self.switch = Switch(path)
        self.agent = agent

    @property
    def on(self) -> bool:
        return self.switch.refresh()

    def set(self, value: bool) -> bool:
        return self.switch.set(value)

    def status(self) -> dict:
        """Everything the page shows, gathered defensively.

        Each fact is checked on its own: the reason somebody opens this page is
        usually that one of them is broken, so a check that cannot run must say so
        rather than take the page down with it.
        """
        facts: dict[str, str] = {}
        from decat.mac import doctor as doctor_module

        try:
            facts["model"] = config.model()
            facts["search"] = config.search_provider()
            facts["space"] = config.space_url()
        except Exception:  # noqa: BLE001
            pass
        for name, ok, detail in doctor_module.quick_checks():
            facts[name] = ("✅ " if ok else "❌ ") + detail
        if self.agent is not None:
            facts["answered this run"] = str(self.agent.handled)
        return {"on": bool(load_state(self.switch._path)["on"]), "facts": facts}


class _Handler(BaseHTTPRequestHandler):
    controller: Controller

    def _send(self, body: bytes, *, status: int = 200, kind: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # A switch page has no business being cached, or framed, or sniffed.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — the base class's spelling
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            body = json.dumps({"on": self.controller.on}).encode()
            return self._send(body, kind="application/json")
        if path in {"/", "/index.html"}:
            return self._send(_page(self.controller))
        return self._send(b"not found", status=404, kind="text/plain")

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/toggle":
            return self._send(b"not found", status=404, kind="text/plain")
        wanted = not self.controller.on
        self.controller.set(wanted)
        body = json.dumps({"on": wanted}).encode()
        return self._send(body, kind="application/json")

    def log_message(self, fmt, *args):  # noqa: A003 — quiet by default
        return


def serve(controller: Controller, *, port: int = PAGE_PORT) -> ThreadingHTTPServer:
    """Start the page. Returns the server so a caller can shut it down."""
    handler = type("_Bound", (_Handler,), {"controller": controller})
    server = ThreadingHTTPServer((PAGE_HOST, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    """The command line: ``decat on|off|status|doctor|run|serve``."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = (args[0] if args else "status").lower()

    if command in {"-h", "--help", "help"}:
        print(_HELP)
        return 0

    if command in {"on", "off"}:
        controller = Controller()
        value = controller.set(command == "on")
        print(f"Decat is now {'ON' if value else 'OFF'} on this Mac.")
        return 0

    if command == "status":
        controller = Controller()
        state = controller.status()
        print(f"Decat is {'ON' if state['on'] else 'OFF'} on this Mac.")
        for name, value in state["facts"].items():
            print(f"  {name}: {value}")
        from decat.mac.store import History

        history = History(config.home() / "history")
        for row in history.recent(3):
            print(f"  · {row.get('lane')} in {row.get('ms', 0) / 1000:.1f}s"
                  f" ({len(row.get('sources') or [])} sources)")
        print(f"  kept this month: {history.count()}")
        return 0

    if command == "doctor":
        from decat.mac import doctor as doctor_module

        return doctor_module.main(args[1:])

    if command in {"run", "serve"}:
        controller = Controller()
        if command == "serve":
            serve(controller)
            print(f"Switch page on http://{PAGE_HOST}:{controller_page_port()} — Ctrl-C to stop.")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            return 0
        if not controller.on:
            print("Decat is OFF on this Mac — run `decat on` first, or flip the page.")
            return 1
        return run_forever(controller)

    print(f"Unknown command {command!r}.\n")
    print(_HELP)
    return 2


def controller_page_port() -> int:
    return PAGE_PORT


def run_forever(controller: Controller) -> int:
    """Answer questions until interrupted, with the page up beside the loop."""
    agent = agent_module.from_config(switch=controller.switch)
    controller.agent = agent
    try:
        serve(controller)
        print(f"[DECAT] switch page on http://{PAGE_HOST}:{PAGE_PORT}")
    except OSError as exc:
        # The page failing to bind is not a reason to stop answering questions:
        # something else is on 8787, and the switch still works from the CLI.
        print(f"[DECAT] switch page not started ({type(exc).__name__})")
    print(f"[DECAT] answering as {agent.switch.to_wire()} — probing {config.space_url()}")
    stop = threading.Event()
    try:
        agent.loop(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\n[DECAT] stopped.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
