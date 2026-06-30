# Device-side keyboard tunnel helper.
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


def _request(op, payload):
    global _NEXT_ID
    _NEXT_ID += 1
    request_id = _NEXT_ID
    _send({"type": "request", "id": request_id, "op": op, "payload": payload})
    while True:
        frame = _recv()
        if frame is None:
            return None
        if frame.get("type") == "response" and frame.get("id") == request_id:
            return frame.get("payload", {}).get("event")


def read_key(timeout_ms=None):
    return _request("kb.read", {"timeout_ms": timeout_ms})


def poll_key():
    return _request("kb.poll", {})


_send({"type": "hello", "id": 0, "op": "kb", "payload": {"version": 1}})

while True:
    event = read_key()
    if event:
        sys.stdout.write("PYRITE_KEY:" + json.dumps(event) + "\n")
        _flush()
