"""Loopback-only HTTP/1.1 fixture; deliberately not a representative benchmark service."""

import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.received = 0
        self.finished = 0
        self.connections = set()

    def snapshot(self):
        with self.lock:
            return dict(active=self.active, peak=self.peak, received=self.received,
                        finished=self.finished, connections=len(self.connections))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address):
        self.state = State()
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        # A disconnected or timed-out client is expected in these experiments.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    wbufsize = 64 * 1024
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/stats":
            self.respond(200, json.dumps(self.server.state.snapshot()).encode())
            return
        state = self.server.state
        with state.lock:
            state.active += 1
            state.received += 1
            sequence = state.received
            state.peak = max(state.peak, state.active)
            state.connections.add(self.client_address)
        try:
            query = parse_qs(parsed.query)
            delay_ms = min(max(float(query.get("delay_ms", [0])[0]), 0), 10000)
            if parsed.path == "/phased":
                delay_ms = 0 if sequence <= 4 else 2000
            if delay_ms and parsed.path != "/slow-body":
                time.sleep(delay_ms / 1000)
            if parsed.path == "/disconnect":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if parsed.path == "/truncated":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"short")
                self.wfile.flush()
                self.close_connection = True
                return
            if parsed.path == "/malformed":
                self.connection.sendall(b"this is not an HTTP response\r\n\r\n")
                self.close_connection = True
                return
            if parsed.path == "/chunked":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"4\r\ntest\r\n3\r\n123\r\n0\r\n\r\n")
                self.wfile.flush()
                return
            status = int(query.get("status", [200])[0])
            if parsed.path == "/mixed":
                status = 503 if sequence % 2 == 0 else 200
            status = status if 200 <= status <= 599 else 500
            size = min(max(int(query.get("bytes", [16])[0]), 0), 1024 * 1024)
            self.respond(status, b"x" * size, close=query.get("close", ["0"])[0] == "1",
                         body_delay_ms=delay_ms if parsed.path == "/slow-body" else 0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            with state.lock:
                state.active -= 1
                state.finished += 1

    def respond(self, status, body, close=False, body_delay_ms=0):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if 300 <= status < 400:
            self.send_header("Location", getattr(self.server, "redirect_to", "/ok"))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if body_delay_ms:
            self.wfile.flush()
            time.sleep(body_delay_ms / 1000)
        # One buffered flush avoids a separate delayed-ACK/Nagle exchange for
        # headers and a tiny body in the fixture itself.
        self.wfile.write(body)
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with Server(("127.0.0.1", args.port)) as server:
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
