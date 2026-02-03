#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDIO_BIN = os.path.join(SCRIPT_DIR, "..", "bin", "claudio")
PORT = int(os.environ.get("PORT", 8421))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/telegram/webhook":
            # Verify webhook secret if configured
            if WEBHOOK_SECRET:
                token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if token != WEBHOOK_SECRET:
                    self._respond(401, {"error": "unauthorized"})
                    return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            self._respond(200, {"ok": True})
            # Process webhook in background
            log_file = os.path.join(
                os.environ.get("HOME", "/tmp"), ".claudio", "webhook.log"
            )
            with open(log_file, "a") as log_fh:
                subprocess.Popen(
                    [CLAUDIO_BIN, "_webhook", body],
                    stdout=log_fh,
                    stderr=log_fh,
                )
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Claudio server listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
