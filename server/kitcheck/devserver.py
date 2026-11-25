"""No-dependency server (Python standard library only), same routes as main.py.

Use it to try KitCheck without installing FastAPI:
    python3 -m kitcheck.devserver --port 8080
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from .api import get_api


class Handler(BaseHTTPRequestHandler):
    server_version = "KitCheck"

    def _run(self):
        api = get_api()
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        status, headers, data = api.handle(
            self.command, u.path, dict(parse_qsl(u.query)),
            {k.lower(): v for k, v in self.headers.items()}, body, self.client_address[0])
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = _run

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    get_api()
    print(f"KitCheck dev server on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
