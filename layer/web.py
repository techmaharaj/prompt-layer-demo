"""The developer's page: an internal assistant, the way one actually looks.

A conversation. The developer says what they want, attaches whatever is in
their workspace, and gets an answer. They never pick a capability; the
platform routes the request (capabilities/*.yaml `match:`), and when nothing
covers it, that is the catalogue saying nobody has decided it yet.

It explains nothing: no owner, no budget, no allowlist, no policy rule. A
refusal is a short reason and a reference, like any platform API error. The
reference links to that exact trace, which is the other screen's job.

Standard library only. The page talks to a small JSON API so sending a message
never reloads anything.
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from . import config as cfg
from . import engine, providers
from .resolve import ROOT, route

CONTEXT_DIR = ROOT / "context"

# Two tools, one process. Which one you get is decided by the path
# (/playground, /assistant) or by the hostname, so putting them on separate
# hostnames later is a Caddy change and not a code change.
MODES = {
    "playground": {
        "title": "Model Playground",
        "tag": "no capability",
        # amber: the ungoverned side, and a different world at a glance.
        "accent": "#e0a23c", "accent_dim": "#8a6520",
        "ink": "#2a1e09", "bubble": "#2a2318", "bubbleline": "#4a3a23",
        "chip": "#2a2118", "chipline": "#4a3a23", "chipink": "#f0cd8a",
    },
    "assistant": {
        "title": "Engineering Assistant",
        "tag": "capabilities",
        "accent": "#2ec4b6", "accent_dim": "#1b8f85",
        "ink": "#07211f", "bubble": "#17302e", "bubbleline": "#26514c",
        "chip": "#122a28", "chipline": "#2c524e", "chipink": "#a9e8e1",
    },
}


def mode_for(path: str, host: str) -> str:
    if "playground" in path or host.startswith("playground"):
        return "playground"
    return "assistant"


@dataclass
class Turn:
    role: str                       # you | assistant | platform
    who: str = ""
    text: str = ""
    files: list[str] = field(default_factory=list)
    reference: str = ""
    trace_url: str = ""


# Two threads, same question. Left goes straight to the model the way it is done
# today; right goes through the capability layer. Both reach the SAME gateway --
# nothing is being replaced, which is the argument.
THREADS: dict[str, list[Turn]] = {"playground": [], "assistant": []}

# One conversation per tool, shared by every viewer, and ThreadingHTTPServer
# will happily run two requests at once. The lock keeps a thread from being
# read while it is half-appended.
LOCK = Lock()


def files_available() -> list[str]:
    if not CONTEXT_DIR.is_dir():
        return []
    return sorted(p.name for p in CONTEXT_DIR.iterdir() if p.is_file())


def _trace_url(trace_id: str) -> str:
    ui = cfg.load().trace_ui
    return f"{ui.rstrip('/')}/trace/{trace_id}" if (ui and trace_id) else ""


def _state(mode: str) -> dict:
    m = MODES[mode]
    return {"thread": [asdict(t) for t in THREADS[mode]],
            "files": files_available(),
            "mode": mode, "title": m["title"], "tag": m["tag"],
            "model": cfg.playground_model()}


def _thread_files(mode: str) -> list[str]:
    """Everything attached so far in this conversation.

    A file attached to a chat belongs to the conversation, not to one message --
    nobody should re-tick a diff to ask a follow-up about it. On the assistant
    side the allowlist still decides what may actually be sent.
    """
    seen: list[str] = []
    for t in THREADS[mode]:
        for f in t.files:
            if f not in seen:
                seen.append(f)
    return seen


def _history(mode: str) -> list[tuple[str, str]]:
    return [(t.role, t.text) for t in THREADS[mode] if t.role in ("you", "assistant")]


def _active_capability(mode: str):
    """The capability currently answering in this thread, if any."""
    from .resolve import load

    for t in reversed(THREADS[mode]):
        if t.role == "assistant" and t.who:
            try:
                return load(t.who)
            except Exception:
                return None
    return None


NO_CONTENT = "The model returned nothing. Nothing is wrong with the layer."
UNREACHABLE = ("The model backend did not answer, so there is no answer to "
               "give you. The decisions above it were still made and recorded.")


def _ask(mode: str, prompt: str, files: list[str]) -> None:
    """Add a turn, and never raise. A backend that is down is a thing the page
    has to say out loud: an HTTP 500 here would read as the layer failing, when
    what failed is the thing behind it."""
    try:
        _ask_inner(mode, prompt, files)
    except providers.ProviderUnreachable as e:
        THREADS[mode].append(Turn("platform", text=f"{UNREACHABLE} ({e})"))
    except Exception as e:                      # noqa: BLE001: see docstring
        THREADS[mode].append(Turn("platform", text=f"{UNREACHABLE} ({type(e).__name__})"))


def _ask_inner(mode: str, prompt: str, files: list[str]) -> None:
    THREADS[mode].append(Turn("you", text=prompt, files=files))

    if mode == "playground":
        # Nothing in the way: the model the developer picked, every file they
        # gathered, no allowlist, no budget, no policy. It still goes through
        # the same gateway, and that is the point.
        model = cfg.playground_model()
        r = engine.run_raw(prompt, _thread_files(mode), model,
                           history=_history(mode)[:-1])
        THREADS[mode].append(Turn("assistant", who=model,
                                  text=r.answer or NO_CONTENT))
        return

    cap = route(prompt) or _active_capability(mode)
    if cap is None:
        THREADS[mode].append(Turn("platform", text=(
            "No capability covers that request yet, so there is nothing for me "
            "to run. If this is something the team needs, it can be proposed in "
            "the capability catalogue.")))
        return

    r = engine.run_capability(cap.name, prompt, _thread_files(mode),
                              history=_history(mode)[:-1])
    if r.outcome == "ok":
        THREADS[mode].append(Turn("assistant", who=cap.name,
                                  text=r.answer or NO_CONTENT))
    else:
        THREADS[mode].append(Turn("platform", text=r.message or UNREACHABLE,
                                  reference=r.reference,
                                  trace_url=_trace_url(r.trace_id)))


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>%TITLE%</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --bg:#0e1013; --nav:#090a0c; --panel:#171a20; --panel2:#1d2129; --line:#262c36;
  --fg:#e7eaf0; --dim:#89919f;
  --accent:%ACCENT%; --accent2:%ACCENT2%; --ink:%INK%;
  --ubg:%UBG%; --uline:%ULINE%;
  --chip:%CHIP%; --chipline:%CHIPLINE%; --chipink:%CHIPINK%;
  --warn:#f0a93b; --warnbg:#231d14;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
  font:17px/1.7 Helvetica,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
header{flex:none;height:58px;background:var(--nav);border-bottom:1px solid var(--line);
  display:flex;align-items:center;gap:14px;padding:0 22px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700}
.dot{width:12px;height:12px;border-radius:3px;background:var(--accent)}
.sp{flex:1}
.ghost{background:none;border:1px solid var(--line);color:var(--dim);border-radius:999px;
  padding:7px 14px;font:13px/1 inherit;cursor:pointer}
main{flex:1;overflow-y:auto;scroll-behavior:smooth}
.thread{max-width:940px;margin:0 auto;padding:32px 24px 8px}
.tag{font-size:11.5px;font-weight:600;border-radius:5px;padding:2px 9px;
  background:var(--chip);border:1px solid var(--chipline);color:var(--chipink)}
.modelpick{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:13px}
.modelpick select{background:var(--panel);color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:5px 9px;font:13px/1 inherit}
.empty{color:var(--dim);text-align:center;padding:90px 0;font-size:18px}
.turn{margin-bottom:26px;display:flex;flex-direction:column}
.turn.you{align-items:flex-end}
.who{font-size:12px;letter-spacing:.9px;text-transform:uppercase;font-weight:700;
  color:var(--accent);margin-bottom:7px}
.who.plat{color:var(--warn)}
.bubble{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:15px 18px;white-space:pre-wrap;max-width:84%;overflow-wrap:anywhere}
.turn.you .bubble{background:var(--ubg);border-color:var(--uline);max-width:74%}
.bubble.plat{background:var(--warnbg);border-color:#50401f;white-space:normal}
.files{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.chip{background:var(--chip);border:1px solid var(--chipline);color:var(--chipink);
  border-radius:6px;padding:3px 10px;font-size:13px}
.ref{margin-top:12px;font-size:14px;color:var(--dim)}
a{color:var(--accent)}
code{background:#0b1a19;border:1px solid #23403d;border-radius:4px;padding:1px 6px;font-size:15px}
.dots span{display:inline-block;width:7px;height:7px;margin-right:4px;border-radius:50%;
  background:var(--dim);animation:b 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes b{0%,60%,100%{opacity:.25}30%{opacity:1}}
footer{flex:none;background:var(--nav);border-top:1px solid var(--line);padding:14px 24px 20px}
.composer{max-width:1100px;margin:0 auto}
.picks{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.pick{font-size:13px;color:var(--dim);border:1px dashed var(--line);border-radius:999px;
  padding:5px 13px;cursor:pointer;user-select:none}
.pick.on{color:var(--chipink);border:1px solid var(--accent2);background:var(--chip)}
.bar{display:flex;gap:10px;align-items:flex-end;background:var(--panel2);
  border:1px solid var(--line);border-radius:14px;padding:8px 8px 8px 16px}
textarea{flex:1;background:none;border:0;color:var(--fg);font:inherit;resize:none;
  outline:none;max-height:180px;padding:9px 0;line-height:1.6}
.send{background:var(--accent);color:var(--ink);border:0;border-radius:10px;width:44px;height:44px;
  font-size:20px;cursor:pointer;flex:none}
.send:disabled{opacity:.4;cursor:default}
.hint{color:var(--dim);font-size:12.5px;margin-top:8px;text-align:center}
</style></head><body>
<header>
  <div class="brand"><span class="dot"></span><span id="title"></span></div>
  <span class="tag" id="tag"></span>
  <div class="sp"></div>
  <div class="modelpick" id="modelpick" style="display:none">
    Model <select id="model"></select>
  </div>
  <button class="ghost" id="new">New chat</button>
</header>
<main id="main"><div class="thread" id="thread"></div></main>
<footer><div class="composer">
  <div class="picks" id="picks"></div>
  <div class="bar">
    <textarea id="box" rows="1" placeholder="What do you need?"></textarea>
    <button class="send" id="send" title="Send">&#8593;</button>
  </div>
  <div class="hint">Enter to send &middot; Shift+Enter for a new line</div>
</div></footer>
<script>
const API = location.pathname.replace(/[/]$/,'') + '/api';
const $=s=>document.querySelector(s), thread=$('#thread'), main=$('#main'),
      box=$('#box'), send=$('#send'), picks=$('#picks');
let picked=new Set(), busy=false;
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// Models emit **bold** and `code` however firmly you ask them not to. Escape
// first, then allow just those two, so nothing user-supplied can inject markup.
const md=s=>esc(s).replace(/[*][*](.+?)[*][*]/g,'<b>$1</b>')
                  .replace(/`([^`]+?)`/g,'<code>$1</code>');
const bottom=()=>main.scrollTop=main.scrollHeight;

function turnHTML(t){
  const files=(t.files||[]).map(f=>`<span class="chip">${esc(f)}</span>`).join('');
  if(t.role==='you') return `<div class="turn you"><div class="bubble">${esc(t.text)}`+
    (files?`<div class="files">${files}</div>`:'')+`</div></div>`;
  const plat=t.role==='platform';
  const ref=t.reference?`<div class="ref">Reference `+
    (t.trace_url?`<a href="${esc(t.trace_url)}" target="_blank">${esc(t.reference)}</a>`
                :esc(t.reference))+`</div>`:'';
  return `<div class="turn"><div class="who${plat?' plat':''}">${esc(plat?'platform':(t.who||'assistant'))}</div>`+
         `<div class="bubble${plat?' plat':''}">${md(t.text)}${ref}</div></div>`;
}

function render(st){
  $('#title').textContent = st.title;
  $('#tag').textContent = st.tag;
  document.title = st.title;
  if(st.mode === 'playground'){
    $('#modelpick').style.display = 'flex';
    $('#model').innerHTML = `<option>${st.model}</option>`;
  }
  thread.innerHTML = st.thread.length ? st.thread.map(turnHTML).join('')
    : '<div class="empty">Ask for what you need.</div>';
  // Sticky, like the server's view: a file attached to this conversation stays
  // attached. Painted from `picked` so the chips can never disagree with what
  // will actually be sent.
  picks.innerHTML = st.files.map(f=>
    `<span class="pick${picked.has(f)?' on':''}" data-f="${esc(f)}">${esc(f)}</span>`).join('');
  bottom();
}
function thinking(on){
  document.querySelectorAll('.pending').forEach(e=>e.remove());
  if(!on) return;
  thread.insertAdjacentHTML('beforeend',
    '<div class="turn pending"><div class="bubble"><span class="dots">'+
    '<span></span><span></span><span></span></span></div></div>');
  bottom();
}
async function load(){ render(await (await fetch(API+'/state')).json()); }

async function ask(){
  const text=box.value.trim(); if(!text||busy) return;
  busy=true; send.disabled=true;
  const files=[...picked];   // server de-duplicates against the thread
  thread.insertAdjacentHTML('beforeend', turnHTML({role:'you',text,files}));
  box.value=''; box.style.height='auto'; bottom(); thinking(true);
  try{
    const r=await fetch(API+'/ask',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({prompt:text,files})});
    thinking(false); render(await r.json());
  }catch(e){ thinking(false);
    thread.insertAdjacentHTML('beforeend', turnHTML(
      {role:'platform',text:'The assistant is unreachable.'})); bottom(); }
  busy=false; send.disabled=false; box.focus();
}

box.addEventListener('input',()=>{box.style.height='auto';
  box.style.height=Math.min(box.scrollHeight,180)+'px';});
box.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault(); ask();}});
send.onclick=ask;
picks.onclick=e=>{const p=e.target.closest('.pick'); if(!p)return;
  const f=p.dataset.f; picked.has(f)?picked.delete(f):picked.add(f);
  p.classList.toggle('on', picked.has(f));};
$('#new').onclick=async()=>{await fetch(API+'/new',{method:'POST'}); picked.clear(); load();};
load(); box.focus();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "platform/1.0"

    def log_message(self, *a):
        pass

    def _mode(self) -> str:
        return mode_for(urllib.parse.urlparse(self.path).path,
                        (self.headers.get("host") or "").lower())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        mode = self._mode()
        if path.endswith("/api/state"):
            return self._json(_state(mode))
        if path == "/healthz":
            return self._send("ok", "text/plain")
        if path in ("/", f"/{mode}"):
            m = MODES[mode]
            page = (PAGE
                    .replace("%ACCENT%", m["accent"]).replace("%ACCENT2%", m["accent_dim"])
                    .replace("%INK%", m["ink"])
                    .replace("%UBG%", m["bubble"]).replace("%ULINE%", m["bubbleline"])
                    .replace("%CHIP%", m["chip"]).replace("%CHIPLINE%", m["chipline"])
                    .replace("%CHIPINK%", m["chipink"])
                    .replace("%TITLE%", m["title"]))
            return self._send(page)
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        mode = self._mode()
        n = int(self.headers.get("content-length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, UnicodeDecodeError):
            return self.send_error(400, "expected a JSON body")
        if not isinstance(body, dict):
            return self.send_error(400, "expected a JSON object")
        if path.endswith("/api/ask"):
            prompt = (body.get("prompt") or "").strip()
            with LOCK:
                if prompt:
                    _ask(mode, prompt, list(body.get("files") or []))
                return self._json(_state(mode))
        if path.endswith("/api/new"):
            with LOCK:
                THREADS[mode].clear()
                return self._json(_state(mode))
        self.send_error(404)

    def _json(self, obj: dict):
        self._send(json.dumps(obj), "application/json")

    def _send(self, body: str, ctype: str = "text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 8088, host: str = "0.0.0.0") -> None:
    """Listens on every interface so the page is reachable from a browser that
    is not on the box. There is no authentication here and the conversation is
    shared by every viewer, so run it somewhere you would not mind a colleague
    finding. Pass a host to narrow it: `python -m layer.web 8088 127.0.0.1`."""
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    import sys

    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8088,
          sys.argv[2] if len(sys.argv) > 2 else "0.0.0.0")
