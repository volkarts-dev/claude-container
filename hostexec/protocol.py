import json
import struct

HEADER_LIMIT = 65536
FRAME_LIMIT = 16 * 1024 * 1024

STDIN = b"I"
STDIN_EOF = b"E"
KILL = b"K"
RESIZE = b"W"
STDOUT = b"O"
STDERR = b"R"
EXIT = b"X"

NEW = b"N"
DATA = b"D"
CLOSE = b"C"
ACCEPT = b"A"
PING = b"P"

_FRAME = struct.Struct(">cI")
_LINK = struct.Struct(">cII")
_STATUS = struct.Struct(">i")


class ProtocolError(Exception):
    pass


def encode_header(message):
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode_header(line):
    try:
        message = json.loads(line.decode("utf-8"))
    except ValueError as error:
        raise ProtocolError(f"malformed header: {error}")
    if not isinstance(message, dict):
        raise ProtocolError("header is not a JSON object")
    return message


def read_header(stream):
    line = stream.readline()
    if not line:
        raise ProtocolError("connection closed before the header arrived")
    if len(line) > HEADER_LIMIT:
        raise ProtocolError("header too long")
    return decode_header(line)


def read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if chunks:
                raise ProtocolError("stream ended inside a frame")
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _payload(stream, length):
    if length > FRAME_LIMIT:
        raise ProtocolError(f"frame of {length} bytes exceeds the limit")
    if not length:
        return b""
    payload = read_exact(stream, length)
    if payload is None:
        raise ProtocolError("stream ended inside a frame")
    return payload


def pack_frame(kind, payload=b""):
    return _FRAME.pack(kind, len(payload)) + payload


def read_frame(stream):
    head = read_exact(stream, _FRAME.size)
    if head is None:
        return None
    kind, length = _FRAME.unpack(head)
    return kind, _payload(stream, length)


def pack_link(kind, session, payload=b""):
    return _LINK.pack(kind, session, len(payload)) + payload


def read_link(stream):
    head = read_exact(stream, _LINK.size)
    if head is None:
        return None
    kind, session, length = _LINK.unpack(head)
    return kind, session, _payload(stream, length)


def pack_status(status):
    return _STATUS.pack(status)


def unpack_status(payload):
    if len(payload) != _STATUS.size:
        raise ProtocolError("malformed exit status")
    return _STATUS.unpack(payload)[0]
