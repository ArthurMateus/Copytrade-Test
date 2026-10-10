"""Daily FOMO picks, step 1b: a one-shot receiver on 127.0.0.1 for the data tools/fomo_collect.js gathered in the
signed-in fomo.family tab. The browser tool cuts long results short and fomo.family's page may not fetch localhost,
so the tab NAVIGATES to http://127.0.0.1:8765/recv#<data> (the #fragment never leaves the browser); that page POSTs
the data here (same origin).

    uv run python tools/fomo_receive.py [day]      (then, in the fomo tab: fomoSend(), see tools/fomo_collect.js)

Writes reports/fomo/<day>/chunk_0.json (the kept traders) and exits. Listens on 127.0.0.1 only, for 10 minutes.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from fomo_picks import day_dir, today  # noqa: E402

PORT = 8765


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else today()
    out = day_dir(day)
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "https://fomo.family")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            page = (b"<!doctype html><meta charset=utf-8><title>FOMO picks receiver</title><p id=s>waiting</p><script>"
                    b"addEventListener('message',e=>{if(e.origin!=='https://fomo.family')return;"
                    b"fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e.data)})"
                    b".then(r=>r.text()).then(t=>{document.getElementById('s').textContent='saved '+t;"
                    b"e.source.postMessage('saved','https://fomo.family')})});"
                    b"if(opener)opener.postMessage('ready','https://fomo.family');"
                    b"if(location.hash.length>1){fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},"
                    b"body:decodeURIComponent(location.hash.slice(1))}).then(r=>r.text()).then(t=>{"
                    b"document.getElementById('s').textContent='saved '+t;history.replaceState(null,'','/recv')})}"
                    b"</script>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            kept = body.get("kept") or {}
            (out / "chunk_0.json").write_text(json.dumps(kept), encoding="utf-8")
            (out / "collect.json").write_text(json.dumps({k: v for k, v in body.items() if k != "kept"}),
                                              encoding="utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"saved": len(kept)}).encode())
            print(f"saved {len(kept)} traders to {out / 'chunk_0.json'}")
            done.set()

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"waiting on http://127.0.0.1:{PORT} (10 min)")
    ok = done.wait(600)
    srv.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
