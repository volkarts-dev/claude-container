import io
import unittest
from pathlib import Path

from hostexec import protocol

ROOT = Path(__file__).resolve().parent.parent


class ProtocolTest(unittest.TestCase):
    def test_header_round_trip(self):
        line = protocol.encode_header({"verb": "build", "args": ["-c", "Release"]})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(protocol.read_header(io.BytesIO(line)), {"verb": "build", "args": ["-c", "Release"]})

    def test_header_errors(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_header(io.BytesIO(b""))
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_header(io.BytesIO(b"[1, 2]\n"))
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_header(io.BytesIO(b"{not json\n"))

    def test_frame_round_trip(self):
        payload = bytes(range(256)) * 3
        stream = io.BytesIO(protocol.pack_frame(protocol.STDOUT, payload) + protocol.pack_frame(protocol.STDIN_EOF))
        self.assertEqual(protocol.read_frame(stream), (protocol.STDOUT, payload))
        self.assertEqual(protocol.read_frame(stream), (protocol.STDIN_EOF, b""))
        self.assertIsNone(protocol.read_frame(stream))

    def test_truncated_frame(self):
        data = protocol.pack_frame(protocol.STDOUT, b"hello")
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_frame(io.BytesIO(data[:-2]))
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_frame(io.BytesIO(data[:3]))

    def test_link_round_trip(self):
        payload = bytes(range(256))
        stream = io.BytesIO(protocol.pack_link(protocol.DATA, 0x01020304, payload) + protocol.pack_link(protocol.PING, 0))
        self.assertEqual(protocol.read_link(stream), (protocol.DATA, 0x01020304, payload))
        self.assertEqual(protocol.read_link(stream), (protocol.PING, 0, b""))
        self.assertIsNone(protocol.read_link(stream))

    def test_frame_limit(self):
        head = protocol.pack_frame(protocol.STDOUT, b"")[:1] + (protocol.FRAME_LIMIT + 1).to_bytes(4, "big")
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_frame(io.BytesIO(head + b"x"))

    def test_status_round_trip(self):
        for status in (0, 1, 255, -15, 2 ** 31 - 1):
            self.assertEqual(protocol.unpack_status(protocol.pack_status(status)), status)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_status(b"\x00")

    def test_container_copy_is_identical(self):
        host = (ROOT / "hostexec" / "protocol.py").read_bytes()
        container = (ROOT / "hostexec" / "container" / "protocol.py").read_bytes()
        self.assertEqual(host, container)


if __name__ == "__main__":
    unittest.main()
