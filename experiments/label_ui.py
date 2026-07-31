"""Browser UI for the blind calibration labeling pass (runbook §7).

Serves `.qa-artifacts/calibration/pending.jsonl` on localhost and writes the
chosen label back into the same file — nothing else changes, so
`judge_calibrate --ingest` works exactly as before. The page shows only what
the pending file contains (id, claim, evidence, label): the blindness
guarantee of Gate 1D-4 is preserved because this server never opens
items.jsonl or anything verdict-derived.

The rubric shown in the page is the judge's own entailment rubric, verbatim.
That is deliberate and is not a leak: human and judge must apply the *same
label definitions* or the agreement number means nothing.

  uv run python -m experiments.label_ui            # http://127.0.0.1:8377
  # label everything in the browser, then:
  uv run python -m experiments.judge_calibrate --ingest
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from experiments.judge_calibrate import CALIBRATION_DIR, LABELS, PENDING_PATH

LOCK = threading.Lock()
PATH: Path = PENDING_PATH   # overridable via --file (tests, future batches)
# Optional second opinion, keyed by id: {"label", "reason", "confidence", "alt"}.
# Never merged into pending.jsonl and hidden until the labeler asks for it —
# a visible suggestion turns "human vs judge" agreement into "did the human
# agree with the assistant", which is a different and much weaker measurement.
ASSIST_PATH: Path = CALIBRATION_DIR / "assist.jsonl"


def load_items() -> list[dict]:
    if not PATH.exists():
        return []
    return [json.loads(line) for line in PATH.read_text().splitlines()
            if line.strip()]


def load_assist() -> dict[str, dict]:
    if not ASSIST_PATH.exists():
        return {}
    out = {}
    for line in ASSIST_PATH.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["id"]] = {k: rec.get(k) for k in
                              ("label", "reason", "confidence", "alt", "by")}
    return out


def save_label(item_id: str, label: str | None) -> bool:
    """Set one label and rewrite the file atomically, all fields preserved."""
    with LOCK:
        items = load_items()
        hit = next((it for it in items if it.get("id") == item_id), None)
        if hit is None:
            return False
        hit["label"] = label
        tmp = PATH.with_name(PATH.name + ".tmp")
        tmp.write_text("".join(json.dumps(it) + "\n" for it in items))
        os.chmod(tmp, 0o600)
        os.replace(tmp, PATH)
        return True


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Judge calibration — blind labeling</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --muted:#68707a;
          --line:#dde1e6; --sup:#1a7f37; --par:#b26a00; --uns:#c62828; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171a; --card:#1e2226; --ink:#e8eaed; --muted:#9aa3ad;
            --line:#33393f; --sup:#4caf7d; --par:#e0a94e; --uns:#ef6e6e; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 system-ui, sans-serif; }
  .wrap { max-width:900px; margin:0 auto; padding:16px 20px 40px; }
  h1 { font-size:17px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:12px; }
  .bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; }
  .bar > div { height:100%; background:var(--sup); width:0; transition:width .2s; }
  .strip { display:flex; flex-wrap:wrap; gap:4px; margin:10px 0 16px; }
  .dot { width:18px; height:18px; border-radius:4px; background:var(--line);
         cursor:pointer; border:2px solid transparent; }
  .dot.cur { border-color:var(--ink); }
  .dot.supported { background:var(--sup); }
  .dot.partial { background:var(--par); }
  .dot.unsupported { background:var(--uns); }
  details { margin-bottom:14px; background:var(--card); border:1px solid var(--line);
            border-radius:8px; padding:10px 14px; font-size:13.5px; }
  details summary { cursor:pointer; font-weight:600; }
  details li { margin:3px 0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 18px; margin-bottom:12px; }
  .tag { font-size:11px; letter-spacing:.06em; text-transform:uppercase;
         color:var(--muted); margin-bottom:6px; }
  .claim { font-size:16.5px; font-weight:600; }
  .evidence { white-space:pre-wrap; overflow-y:auto; max-height:52vh;
              font-size:14px; }
  .evidence .ref { font-weight:700; color:var(--par); }
  .btns { display:flex; gap:10px; margin-top:14px; flex-wrap:wrap; }
  button { font:600 14px system-ui; padding:10px 18px; border-radius:8px;
           border:2px solid var(--line); background:var(--card); color:var(--ink);
           cursor:pointer; }
  button.on-supported { border-color:var(--sup); color:var(--sup); }
  button.on-partial { border-color:var(--par); color:var(--par); }
  button.on-unsupported { border-color:var(--uns); color:var(--uns); }
  button.active { color:#fff; }
  button.active.on-supported { background:var(--sup); }
  button.active.on-partial { background:var(--par); }
  button.active.on-unsupported { background:var(--uns); }
  .assist { margin-top:14px; border:1px dashed var(--line); border-radius:8px;
            padding:10px 14px; background:var(--card); font-size:14px; }
  .assist summary { cursor:pointer; color:var(--muted); font-weight:600;
                    font-size:13px; }
  .assist .verdict { font-weight:700; }
  .assist .verdict.supported { color:var(--sup); }
  .assist .verdict.partial { color:var(--par); }
  .assist .verdict.unsupported { color:var(--uns); }
  .assist p { margin:8px 0 0; }
  .assist .caveat { color:var(--muted); font-size:12.5px; margin-top:8px; }
  .nav { color:var(--muted); font-size:13px; margin-top:10px; }
  kbd { background:var(--line); border-radius:3px; padding:0 5px; font-size:12px; }
  .done { background:var(--sup); color:#fff; border-radius:8px; padding:12px 16px;
          margin-bottom:14px; font-weight:600; display:none; }
  .done code { background:rgba(0,0,0,.25); padding:2px 6px; border-radius:4px; }
  .meta { color:var(--muted); font-size:12px; float:right; }
</style></head><body><div class="wrap">
  <h1>Judge calibration — blind labeling</h1>
  <div class="sub"><span id="progress"></span> · label the CLAIM against the
    EVIDENCE only. What you know about the world doesn’t count.</div>
  <div class="bar"><div id="fill"></div></div>
  <div class="strip" id="strip"></div>
  <div class="done" id="done">All items labeled 🎉 — now run:
    <code>uv run python -m experiments.judge_calibrate --ingest</code></div>
  <details open><summary>Rubric (same one the judge uses)</summary><ul>
    <li><b>supported</b> — every factual element of the claim follows from the evidence.</li>
    <li><b>partial</b> — the evidence supports part of the claim but not all of it,
        or supports it in weaker terms than the claim states.</li>
    <li><b>unsupported</b> — the evidence does not establish the claim, contradicts
        it, or is about something else.</li>
  </ul>Judge only against the evidence given — a claim that is true in the world
  but absent from the evidence is <b>unsupported</b>. Numbers, names and dates
  must match: “68” is not support for “78”.</details>
  <div class="card"><span class="meta" id="itemid"></span>
    <div class="tag">Claim</div><div class="claim" id="claim"></div></div>
  <div class="card"><div class="tag">Evidence (exactly what the judge saw)</div>
    <div class="evidence" id="evidence"></div></div>
  <div class="btns" id="btns"></div>
  <div id="assistbox"></div>
  <div class="nav"><kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> label &amp; advance ·
    <kbd>←</kbd>/<kbd>→</kbd> navigate · <kbd>Backspace</kbd> clear label ·
    <kbd>r</kbd> reveal second opinion</div>
</div><script>
let items = [], labels = [], assist = {}, cur = 0;

const revealed = new Set();   // per-item: collapsed again on every new item

function esc(s) { const d = document.createElement('div');
  d.textContent = s; return d.innerHTML; }

function render() {
  const total = items.length, done = items.filter(i => i.label).length;
  document.getElementById('progress').textContent = done + ' / ' + total + ' labeled';
  document.getElementById('fill').style.width = total ? (100 * done / total) + '%' : 0;
  document.getElementById('done').style.display =
    (total && done === total) ? 'block' : 'none';
  const strip = document.getElementById('strip');
  strip.innerHTML = '';
  items.forEach((it, k) => {
    const d = document.createElement('div');
    d.className = 'dot ' + (it.label || '') + (k === cur ? ' cur' : '');
    d.title = it.id + (it.label ? ' — ' + it.label : '');
    d.onclick = () => { cur = k; render(); };
    strip.appendChild(d);
  });
  const it = items[cur];
  if (!it) return;
  document.getElementById('itemid').textContent =
    it.id + '  (' + (cur + 1) + ' of ' + total + ')';
  document.getElementById('claim').textContent = it.claim;
  document.getElementById('evidence').innerHTML =
    esc(it.evidence).replace(/\[(\d+)\]/g, '<span class="ref">[$1]</span>');
  document.querySelector('.evidence').scrollTop = 0;
  const btns = document.getElementById('btns');
  btns.innerHTML = '';
  labels.forEach((lab, k) => {
    const b = document.createElement('button');
    b.className = 'on-' + lab + (it.label === lab ? ' active' : '');
    b.textContent = (k + 1) + ' · ' + lab;
    b.onclick = () => setLabel(lab);
    btns.appendChild(b);
  });
  renderAssist(it);
}

function renderAssist(it) {
  const box = document.getElementById('assistbox');
  const a = assist[it.id];
  box.innerHTML = '';
  if (!a) return;
  const d = document.createElement('details');
  d.className = 'assist';
  d.open = revealed.has(it.id);
  d.ontoggle = () => { d.open ? revealed.add(it.id) : revealed.delete(it.id); };
  const s = document.createElement('summary');
  s.textContent = '🤔 Second opinion (' + (a.by || 'assistant') +
    ') — click or press r to reveal';
  d.appendChild(s);
  const body = document.createElement('div');
  body.innerHTML =
    '<p><span class="verdict ' + a.label + '">' + a.label + '</span>' +
    (a.confidence ? ' <span class="caveat">· confidence ' + a.confidence + '</span>' : '') +
    (a.alt ? ' <span class="caveat">· defensible alternative: ' + a.alt + '</span>' : '') +
    '</p><p>' + esc(a.reason || '') + '</p>' +
    '<p class="caveat">Judged blind to the judge’s verdict, same as you. ' +
    'It is a second reader, not an answer key — where you disagree, you are ' +
    'the ground truth.</p>';
  d.appendChild(body);
  box.appendChild(d);
}

async function setLabel(lab) {
  const it = items[cur];
  await fetch('/api/label', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: it.id, label: lab }) });
  it.label = lab;
  if (lab) {
    let n = items.findIndex((x, k) => k > cur && !x.label);
    if (n < 0) n = items.findIndex(x => !x.label);
    if (n >= 0) cur = n;
  }
  render();
}

document.addEventListener('keydown', e => {
  if (e.key >= '1' && e.key <= String(labels.length))
    setLabel(labels[Number(e.key) - 1]);
  else if (e.key === 'ArrowRight') { cur = Math.min(cur + 1, items.length - 1); render(); }
  else if (e.key === 'ArrowLeft') { cur = Math.max(cur - 1, 0); render(); }
  else if (e.key === 'Backspace') { e.preventDefault(); setLabel(null); }
  else if (e.key === 'r' || e.key === 'R') {
    const id = items[cur] && items[cur].id;
    if (!id) return;
    revealed.has(id) ? revealed.delete(id) : revealed.add(id);
    renderAssist(items[cur]);
  }
});

fetch('/api/items').then(r => r.json()).then(d => {
  items = d.items; labels = d.labels; assist = d.assist || {};
  const n = items.findIndex(i => !i.label);
  cur = n >= 0 ? n : 0;
  render();
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/items":
            self._send(200, {"items": load_items(), "labels": list(LABELS),
                             "assist": load_assist()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/api/label":
            self._send(404, {"error": "not found"})
            return
        try:
            req = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
        except (ValueError, TypeError):
            self._send(400, {"error": "bad json"})
            return
        label = req.get("label")
        if label is not None and label not in LABELS:
            self._send(400, {"error": f"label must be one of {LABELS} or null"})
            return
        if not save_label(req.get("id", ""), label):
            self._send(404, {"error": f"no item with id {req.get('id')!r}"})
            return
        items = load_items()
        self._send(200, {"ok": True, "total": len(items),
                         "labeled": sum(1 for i in items if i.get("label"))})

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass


def main() -> None:
    global PATH
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--file", type=Path, default=PENDING_PATH,
                    help="pending.jsonl to serve (default: the real one)")
    args = ap.parse_args()
    PATH = args.file

    items = load_items()
    if not items:
        raise SystemExit(f"nothing to label in {PATH} — run "
                         "`judge_calibrate --sample` first")
    todo = sum(1 for i in items if not i.get("label"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {len(items)} item(s) ({todo} unlabeled) from {PATH}")
    print(f"open   http://127.0.0.1:{args.port}")
    print("labels save on click; when done: "
          "uv run python -m experiments.judge_calibrate --ingest")
    server.serve_forever()


if __name__ == "__main__":
    main()
