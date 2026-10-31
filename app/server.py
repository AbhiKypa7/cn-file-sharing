"""Concurrent TCP file server.

    python3 app/server.py                 # listens on 0.0.0.0:5000

Design in one breath:
  * one listening TCP socket; accept() hands back a NEW socket per client
  * each client gets its own thread  -> many transfers at the same time
  * nothing works until LOGIN succeeds
  * uploads land in a temp file first, are checksum-verified, then swapped
    into place under a per-file lock (os.replace is atomic)
"""
import argparse
import logging
import os
import socket
import threading
import uuid

from common import MAX_UPLOAD, PORT, recv_msg, recv_to_file, send_msg, send_stream
from storage import DEFAULT_ROOT, Storage

log = logging.getLogger("server")


def safe_name(name):
    """Strip any folder parts so a client can't write to ../../etc/passwd."""
    name = os.path.basename(str(name or "")).strip()
    if not name or name in (".", "..") or name.startswith(".") or len(name) > 200:
        return None
    return name


class FileServer:
    def __init__(self, host, port, root):
        self.addr = (host, port)
        self.files_dir = os.path.join(root, "files")
        self.tmp_dir = os.path.join(root, "tmp")
        os.makedirs(self.files_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.storage = Storage(os.path.join(root, "meta.db"))
        self._file_locks = {}            # filename -> Lock
        self._guard = threading.Lock()   # protects _file_locks and _active
        self._active = 0

    # ---------- helpers ----------
    def _lock_for(self, name):
        with self._guard:
            if name not in self._file_locks:
                self._file_locks[name] = threading.Lock()
            return self._file_locks[name]

    def _change_active(self, delta):
        with self._guard:
            self._active += delta
            return self._active

    # ---------- main loop ----------
    def serve_forever(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)        # SOCK_STREAM = TCP
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)      # restart without "address in use"
        srv.bind(self.addr)
        srv.listen(64)                                                  # up to 64 waiting connections
        log.info("file server listening on %s:%d", *self.addr)
        while True:
            conn, addr = srv.accept()                                   # blocks until a client connects
            t = threading.Thread(target=self._handle, args=(conn, addr),
                                 name="client-%s:%d" % addr, daemon=True)
            t.start()                                                   # main loop goes straight back to accept()

    # ---------- one client ----------
    def _handle(self, conn, addr):
        log.info("connected (%d active clients)", self._change_active(+1))
        conn.settimeout(300)          # drop clients that go silent for 5 minutes
        rfile = conn.makefile("rb")   # buffered reader: lets us read "one line" or "exactly N bytes"
        user = None
        try:
            while True:
                msg = recv_msg(rfile)
                if msg is None:
                    break                                    # client hung up
                cmd = str(msg.get("cmd", "")).upper()

                if cmd == "LOGIN":
                    username = str(msg.get("user", ""))
                    if self.storage.check_login(username, str(msg.get("pass", ""))):
                        user = username
                        log.info("login OK: %s", user)
                        send_msg(conn, {"ok": True})
                    else:
                        log.info("login FAILED for %r", username)
                        send_msg(conn, {"ok": False, "error": "invalid username or password"})
                elif cmd == "QUIT":
                    send_msg(conn, {"ok": True})
                    break
                elif user is None:
                    send_msg(conn, {"ok": False, "error": "login required"})
                    break                                    # not logged in -> disconnect
                elif cmd == "LIST":
                    send_msg(conn, {"ok": True, "files": self.storage.list_files()})
                elif cmd == "UPLOAD":
                    self._upload(conn, rfile, user, msg)
                elif cmd == "DOWNLOAD":
                    self._download(conn, msg)
                else:
                    send_msg(conn, {"ok": False, "error": "unknown command %r" % cmd})
        except (OSError, ValueError) as e:                   # broken pipe, timeout, bad JSON ...
            log.warning("connection error: %s", e)
        finally:
            rfile.close()
            conn.close()
            log.info("disconnected (%d active clients)", self._change_active(-1))

    def _upload(self, conn, rfile, user, msg):
        name = safe_name(msg.get("name"))
        try:
            size = int(msg.get("size", -1))
        except (TypeError, ValueError):
            size = -1
        if name is None or not 0 <= size <= MAX_UPLOAD:
            send_msg(conn, {"ok": False, "error": "invalid file name or size"})
            return

        # Tell the client "go ahead" BEFORE it sends data, so a rejected upload wastes no bandwidth.
        send_msg(conn, {"ok": True, "ready": True})

        # 1) Receive into a private temp file. No lock held: the slow network part runs in parallel.
        tmp = os.path.join(self.tmp_dir, uuid.uuid4().hex)
        try:
            digest = recv_to_file(rfile, size, tmp)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

        # 2) Integrity check: what arrived must match the fingerprint the client computed.
        expected = msg.get("sha256")
        if expected and expected != digest:
            os.remove(tmp)
            send_msg(conn, {"ok": False, "error": "checksum mismatch, upload discarded"})
            return

        # 3) Swap into place + record metadata as ONE step under the file's lock.
        with self._lock_for(name):
            os.replace(tmp, os.path.join(self.files_dir, name))
            self.storage.save_file_info(name, size, user, digest)
        log.info("UPLOAD   %s (%d bytes) by %s", name, size, user)
        send_msg(conn, {"ok": True, "sha256": digest})

    def _download(self, conn, msg):
        name = safe_name(msg.get("name"))
        f, info, size = None, None, 0
        if name:
            # Under the lock: read metadata and open the file together, so they always match.
            with self._lock_for(name):
                info = self.storage.get_file_info(name)
                path = os.path.join(self.files_dir, name)
                if info and os.path.exists(path):
                    f = open(path, "rb")
                    size = os.fstat(f.fileno()).st_size
        if f is None:
            send_msg(conn, {"ok": False, "error": "no such file"})
            return
        # Lock released: even if someone replaces the file now, our open handle still
        # points to the old complete copy, so the download can never be half-old/half-new.
        with f:
            send_msg(conn, {"ok": True, "size": size, "sha256": info["sha256"]})
            send_stream(conn, f, size)
        log.info("DOWNLOAD %s (%d bytes)", name, size)


def main():
    p = argparse.ArgumentParser(description="Concurrent TCP file server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--root", default=DEFAULT_ROOT, help="where files + metadata are stored")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(message)s")
    try:
        FileServer(args.host, args.port, args.root).serve_forever()
    except KeyboardInterrupt:
        log.info("server stopped")


if __name__ == "__main__":
    main()
