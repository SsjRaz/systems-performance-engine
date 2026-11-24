"""Loopback service with a controlled delay for the Git revision study example."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import time

from cost import DELAY_MS


class Catalog(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def do_GET(self):
        if self.path == "/ready":
            body = b"ready\n"
        elif self.path == "/lookup":
            time.sleep(DELAY_MS / 1000)
            body = b'{"answer":42}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


with ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Catalog) as server:
    server.serve_forever()
