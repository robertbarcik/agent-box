#!/usr/bin/env python3
"""Door 1 of the agent-box: the only way from the box to the model provider.

The guard does four things, in this order:

1. Refuses every path that is not a conversation with the model.
2. Reads the message and refuses what the agent may not decide for itself:
   another model than the one you chose, and any tool that runs on the
   provider's side (web search above all).
3. Adds what only the guard holds: the real API key and, when YOU switched it
   on for this run, the web search tool.
4. Writes one line per request into logs/gateway.log.

Standard library only, so it can be read top to bottom.
"""
import datetime as dt
import http.client
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "openrouter.ai"
KEY = os.environ.get("OPENROUTER_API_KEY", "")
LOG_PATH = "/box-logs/gateway.log"
WEB_SWITCH = "/box-policy/web"          # written by bin/box on the host; the agent cannot see it
MAX_BODY = 50 * 1024 * 1024
WEB_TOOL = "openrouter:web_search"

# The models the agent may talk to: the one in BOX_MODEL, plus BOX_EXTRA_MODELS if you set it.
ALLOWED_MODELS = set()
for _m in [os.environ.get("BOX_MODEL", "")] + os.environ.get("BOX_EXTRA_MODELS", "").split(","):
    _m = _m.strip()
    if _m.startswith("openrouter/"):
        _m = _m[len("openrouter/"):]
    if _m:
        ALLOWED_MODELS.add(_m)

GET_PATHS = re.compile(r"^/api/v1/models(/[A-Za-z0-9._:/-]*)?$")
POST_PATHS = re.compile(r"^/api/v1/(chat/completions|responses)$")
# Never forwarded: they belong to this hop, or would carry the agent's own identity upstream.
DROP_REQUEST = {"host", "authorization", "cookie", "content-length", "connection", "proxy-authorization",
                "transfer-encoding", "accept-encoding", "x-forwarded-for"}
PASS_RESPONSE = {"content-type", "cache-control", "retry-after"}

_log_lock = threading.Lock()


def web_is_on():
    try:
        with open(WEB_SWITCH) as fh:
            return fh.read().strip() == "on"
    except OSError:
        return False


def clean(text, limit=120):
    """Anything the agent sent, made safe to sit on one log line."""
    return re.sub(r"[^A-Za-z0-9._:/@+-]", "?", str(text))[:limit] or "-"


def judge(body):
    """Return (reason to refuse or None, model, the web switch) for one message to the model."""
    web = web_is_on()
    model = body.get("model")
    if not isinstance(model, str) or model not in ALLOWED_MODELS:
        return "model-not-allowed", model, web
    if body.get("models") or body.get("plugins") or body.get("web_search_options"):
        return "provider-side-feature", model, web
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        return "bad-tools", model, web
    for tool in tools:
        kind = tool.get("type") if isinstance(tool, dict) else None
        # "function" tools run inside the box, under its walls. Every other kind runs on the
        # provider's side, outside them. Those are the guard's to add, never the agent's.
        if kind != "function":
            return "provider-side-tool", model, web
    if web:
        body["tools"] = tools + [{"type": WEB_TOOL}]
    return None, model, web


class Guard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-box-gateway"
    sys_version = ""

    def log_message(self, *args):       # the access log below replaces the default one
        pass

    def write_log(self, status, upstream="-", sent=0, model=None, web=None, reason=None):
        secs = (dt.datetime.now(dt.timezone.utc) - self.started).total_seconds()
        line = "%s %s %s status=%s upstream=%s secs=%.3f bytes_in=%d bytes_out=%d" % (
            self.started.strftime("%Y-%m-%dT%H:%M:%S+00:00"), clean(self.command, 10),
            clean(self.path.split("?")[0]), status, upstream, secs, self.bytes_in, sent)
        if model is not None:
            line += " model=%s web=%s" % (clean(model), "on" if web else "off")
        if reason:
            line += " refused=%s" % reason
        with _log_lock:
            with open(LOG_PATH, "a") as fh:
                fh.write(line + "\n")

    def refuse(self, status, text, **log):
        data = ("agent-box gateway: %s\n" % text).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True
        self.write_log(status, sent=len(data), **log)

    def handle_any(self):
        self.started = dt.datetime.now(dt.timezone.utc)
        self.bytes_in = 0
        path = self.path.split("?")[0]
        if self.command == "GET" and path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        # One spelling per path. Encoded characters and dot segments are how a request is made
        # to look allowed here and mean something else upstream.
        if "%" in path or ".." in path or "//" in path or "\\" in path or ";" in path:
            return self.refuse(403, "this path is not allowed")
        if self.command == "GET" and GET_PATHS.match(path):
            return self.forward(None)
        if self.command == "POST" and POST_PATHS.match(path):
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.refuse(411, "a Content-Length is required")
            if length < 0 or length > MAX_BODY:
                return self.refuse(413, "message too large")
            raw = self.rfile.read(length)
            self.bytes_in = len(raw)
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self.refuse(400, "the message is not a JSON object")
            reason, model, web = judge(body)
            if reason:
                return self.refuse(403, "refused (%s). The model and web search are chosen outside the box."
                                   % reason, model=model, web=web, reason=reason)
            return self.forward(json.dumps(body).encode(), model, web)
        return self.refuse(403, "this path is not allowed")

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = handle_any

    def forward(self, data, model=None, web=None):
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_REQUEST}
        headers["Host"] = UPSTREAM
        headers["Authorization"] = "Bearer " + KEY          # the one line this whole file exists for
        headers["Accept-Encoding"] = "identity"
        if data is not None:
            headers["Content-Length"] = str(len(data))
        sent, status = 0, "-"
        conn = http.client.HTTPSConnection(UPSTREAM, timeout=900)
        try:
            conn.request(self.command, self.path, body=data, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            self.send_response(status)
            for name, value in resp.getheaders():
                if name.lower() in PASS_RESPONSE:
                    self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:                 # pass the answer on as it arrives (token streaming)
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                sent += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.write_log(status, upstream=status, sent=sent, model=model, web=web)
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            if status == "-":           # nothing sent yet: tell the agent the line is down
                try:
                    self.refuse(502, "the model provider could not be reached", model=model, web=web)
                except OSError:
                    pass
            else:
                self.write_log(status, upstream="cut", sent=sent, model=model, web=web)
        finally:
            conn.close()


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", 8080), Guard).serve_forever()
