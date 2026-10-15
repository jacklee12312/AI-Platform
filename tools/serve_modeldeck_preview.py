"""Loopback-only SPA server for local UI tests; no device operations."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = Path(self.translate_path(self.path))
        if not path.exists() and not Path(urlsplit(self.path).path).suffix and not self.path.startswith('/api/'):
            self.path = '/index.html'
        super().do_GET()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=18712)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]/'platform-overlays/1.2.0/modeldeck-frontend/dist'
    ThreadingHTTPServer(('127.0.0.1',args.port), partial(Handler,directory=str(root))).serve_forever()
