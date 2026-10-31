"""Shared protocol helpers used by BOTH the server and the client.

WIRE FORMAT (the "contract" between client and server)
------------------------------------------------------
Every message is ONE line of JSON ending in "\n".
If a message announces a file, exactly `size` raw bytes follow it.

    {"cmd": "UPLOAD", "name": "a.pdf", "size": 1048576, "sha256": "..."}\n
    <1048576 raw bytes>

Why? TCP is a BYTE STREAM, not a message stream. It does not remember where
one send() ended and the next began. So the receiver must be told exactly
how many bytes belong to the file; otherwise it cannot know where the file
stops and the next command starts. This is called "length-prefix framing".
"""
import hashlib
import json

SERVER_IP = "10.0.0.1"          # h1 inside Mininet
PORT = 5000                     # TCP port of the file service
CHUNK = 64 * 1024               # move data in 64 KB pieces (never load whole file in RAM)
MAX_HEADER = 64 * 1024          # a JSON header longer than this is treated as an attack/bug
MAX_UPLOAD = 2 * 1024 ** 3      # 2 GB per file


def send_msg(sock, msg):
    """Send one JSON header line."""
    sock.sendall((json.dumps(msg) + "\n").encode())


def recv_msg(rfile):
    """Read one JSON header line. Returns None if the other side closed the connection."""
    line = rfile.readline(MAX_HEADER + 1)
    if not line:
        return None
    if len(line) > MAX_HEADER or not line.endswith(b"\n"):
        raise ValueError("header too long or malformed")
    return json.loads(line.decode())


def send_stream(sock, fileobj, size):
    """Send exactly `size` bytes from an open file, chunk by chunk."""
    remaining = size
    while remaining > 0:
        chunk = fileobj.read(min(CHUNK, remaining))
        if not chunk:
            raise IOError("file ended early while sending")
        sock.sendall(chunk)
        remaining -= len(chunk)


def recv_to_file(rfile, size, path):
    """Receive exactly `size` bytes into `path`. Returns the SHA-256 of what arrived."""
    h = hashlib.sha256()
    remaining = size
    with open(path, "wb") as out:
        while remaining > 0:
            chunk = rfile.read(min(CHUNK, remaining))
            if not chunk:
                raise ConnectionError("connection closed mid-transfer (%d bytes missing)" % remaining)
            out.write(chunk)
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def file_sha256(path):
    """Fingerprint of a file. Same bytes -> same fingerprint; one bit changed -> totally different."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
