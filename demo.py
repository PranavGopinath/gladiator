"""Credential-free stand-in: serves a page while continually updating a file."""
import json
from pathlib import Path
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

public = Path("public")
public.mkdir(exist_ok=True)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(public), **kwargs)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
counter = 0
while True:
    counter += 1
    record = {"demo_tick": counter, "time": time.time()}
    (public / "index.html").write_text("<h1>Agent arena demo</h1><pre>" + json.dumps(record) + "</pre>")
    print(json.dumps(record), flush=True)
    time.sleep(1)
