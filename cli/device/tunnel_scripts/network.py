# Device-side network tunnel helper.
import sys

try:
    import ujson as json
except ImportError:
    import json

_PREFIX = "PYRITE_TUNNEL "
_NEXT_ID = 0


def _flush():
    flush = getattr(sys.stdout, "flush", None)
    if flush:
        flush()


def _send(frame):
    sys.stdout.write(_PREFIX + json.dumps(frame) + "\n")
    _flush()


def _recv():
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.startswith(_PREFIX):
            return json.loads(line[len(_PREFIX):])


def request(method, url, headers=None, body_b64=None):
    global _NEXT_ID
    _NEXT_ID += 1
    payload = {
        "method": method,
        "url": url,
        "headers": headers or {},
    }
    if body_b64 is not None:
        payload["body_b64"] = body_b64
    _send({"type": "request", "id": _NEXT_ID, "op": "http", "payload": payload})
    while True:
        frame = _recv()
        if frame is None:
            return None
        if frame.get("id") == _NEXT_ID:
            if frame.get("type") == "error":
                raise RuntimeError(frame.get("payload", {}).get("message", "tunnel error"))
            return frame.get("payload", {})


def get(url, headers=None):
    return request("GET", url, headers=headers)


def post(url, headers=None, body_b64=None):
    return request("POST", url, headers=headers, body_b64=body_b64)


def _emit_result(payload):
    sys.stdout.write("PYRITE_TUNNEL_RESULT " + json.dumps(payload) + "\n")
    _flush()


def _emit_error(message):
    _emit_result({"error": message})


def _handle_command(line):
    line = line.strip()
    if not line:
        return
    cmd = line.split(None, 2)
    method = cmd[0].upper()
    if method in ("GET", "DELETE"):
        if len(cmd) < 2:
            _emit_error(method + " requires a URL")
            return
        url = cmd[1]
        _emit_result(request(method, url))
        return
    if method in ("POST", "PUT"):
        if len(cmd) < 2:
            _emit_error(method + " requires a URL")
            return
        url = cmd[1]
        body_b64 = cmd[2] if len(cmd) > 2 else ""
        _emit_result(request(method, url, body_b64=body_b64))
        return
    _emit_error("unsupported method: " + method)


_send({"type": "hello", "id": 0, "op": "network", "payload": {"version": 1}})

while True:
    line = sys.stdin.readline()
    if not line:
        break
    if line.startswith(_PREFIX):
        continue
    _handle_command(line)
