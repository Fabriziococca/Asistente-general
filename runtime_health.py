"""Estado de salud sin consultas a proveedores por cada request HTTP."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
import json
import time


class HealthState:
    def __init__(self):
        self._lock = Lock()
        self._state = dict(discord=False, database=False, tickets=False, commands=False)
        self._heartbeat = time.monotonic()

    def update(self, **fields):
        with self._lock:
            self._state.update(fields)
            self._heartbeat = time.monotonic()

    def snapshot(self):
        with self._lock:
            result = dict(self._state)
            result['event_loop'] = time.monotonic() - self._heartbeat < 60
        result['ready'] = all(result.values())
        return result


def health_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            snapshot = state.snapshot()
            if self.path not in ('/', '/healthz', '/readyz'):
                self.send_error(404)
                return
            healthy = snapshot['event_loop'] if self.path == '/healthz' else snapshot['ready']
            payload = json.dumps(snapshot).encode()
            self.send_response(200 if healthy else 503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(payload)

        do_HEAD = do_GET

        def log_message(self, *args):
            pass
    return Handler


def start_health_server(state, port=8080):
    server = ThreadingHTTPServer(('0.0.0.0', port), health_handler(state))
    Thread(target=server.serve_forever, name='health-http', daemon=True).start()
    return server
