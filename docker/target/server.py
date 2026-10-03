import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SECRET = Path('/run/secrets/flag').read_text().strip()
APP_TOKEN = Path('/run/secrets/app_token').read_text().strip()
PUBLIC = {'id': 'public', 'content': 'Welcome to the document service.'}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/documents/public':
            self.send_json(200, PUBLIC)
        elif path == '/documents/private':
            # Authorization is intentionally missing. Preserve authenticated access when repairing.
            self.send_json(200, {'id': 'private', 'content': SECRET})
        else:
            self.send_json(404, {'error': 'Not found'})

    def send_json(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
