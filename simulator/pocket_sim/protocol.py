import json

V = 2


def msg(type_, **fields):
    d = {"type": type_, "v": V}
    d.update(fields)
    return d


def encode(m):
    return json.dumps(m)


def decode(raw):
    m = json.loads(raw)
    if not isinstance(m, dict):
        raise ValueError("message must be an object")
    if m.get("v") != V:
        raise ValueError("protocol version mismatch")
    if not isinstance(m.get("type"), str):
        raise ValueError("missing type")
    return m
