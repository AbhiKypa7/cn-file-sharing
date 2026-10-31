"""TCP file-sharing client.

    python3 app/client.py --user alice --password alice123 list
    python3 app/client.py --user alice --password alice123 upload notes.pdf [--as other_name.pdf]
    python3 app/client.py --user alice --password alice123 download notes.pdf [--out downloads]
    python3 app/client.py --user alice --password alice123 shell      # interactive, good for demos

Exit codes: 0 ok | 1 operation failed | 2 could not connect (e.g. blocked by SDN) | 3 login rejected
"""
import argparse
import csv
import os
import socket
import sys
import time

from common import PORT, SERVER_IP, file_sha256, recv_msg, recv_to_file, send_msg, send_stream


class FileClient:
    def __init__(self, host, port, connect_timeout=5.0):
        # create_connection = socket() + connect(): this is where the TCP 3-way handshake happens.
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        self.sock.settimeout(300)
        self.rfile = self.sock.makefile("rb")

    def _request(self, msg):
        send_msg(self.sock, msg)
        resp = recv_msg(self.rfile)
        if resp is None:
            raise ConnectionError("server closed the connection")
        return resp

    def login(self, user, password):
        return self._request({"cmd": "LOGIN", "user": user, "pass": password})

    def list_files(self):
        resp = self._request({"cmd": "LIST"})
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error"))
        return resp["files"]

    def upload(self, path, remote_name=None):
        size = os.path.getsize(path)
        sha = file_sha256(path)                      # fingerprint computed before timing starts
        name = remote_name or os.path.basename(path)
        start = time.time()
        resp = self._request({"cmd": "UPLOAD", "name": name, "size": size, "sha256": sha})
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error"))   # rejected before any data was sent
        with open(path, "rb") as f:
            send_stream(self.sock, f, size)
        final = recv_msg(self.rfile)
        end = time.time()
        if not final or not final.get("ok"):
            raise RuntimeError((final or {}).get("error", "no confirmation from server"))
        return {"name": name, "size": size, "start": start, "end": end}

    def download(self, name, out_dir="."):
        os.makedirs(out_dir, exist_ok=True)
        start = time.time()
        resp = self._request({"cmd": "DOWNLOAD", "name": name})
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error"))
        size = int(resp["size"])
        final_path = os.path.join(out_dir, os.path.basename(name))
        tmp = final_path + ".part"
        try:
            digest = recv_to_file(self.rfile, size, tmp)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        end = time.time()
        if digest != resp["sha256"]:
            os.remove(tmp)
            raise RuntimeError("checksum mismatch - file corrupted in transit")
        os.replace(tmp, final_path)
        return {"name": name, "size": size, "start": start, "end": end, "path": final_path}

    def close(self):
        try:
            self._request({"cmd": "QUIT"})
        except OSError:
            pass
        self.rfile.close()
        self.sock.close()


# ---------------- result logging (used by the tests / graphs) ----------------
LOG_FIELDS = ["timestamp", "tag", "client_id", "op", "file", "size_bytes",
              "start_ts", "end_ts", "duration_s", "throughput_mbps", "ok", "error"]


def log_result(path, row):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def make_row(args, op, name, result=None, error=""):
    row = {k: "" for k in LOG_FIELDS}
    row.update(timestamp="%.3f" % time.time(), tag=args.tag, client_id=args.id, op=op, file=name,
               ok=0 if error else 1, error=error)
    if result:
        dur = max(result["end"] - result["start"], 1e-9)
        row.update(size_bytes=result["size"], start_ts="%.3f" % result["start"], end_ts="%.3f" % result["end"],
                   duration_s="%.3f" % dur, throughput_mbps="%.3f" % (result["size"] * 8 / dur / 1e6))
    return row


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def print_files(files):
    if not files:
        print("(no files on server)")
    for f in files:
        print("  %-30s %10s  owner=%-8s %s" % (f["name"], human(f["size"]), f["owner"], f["uploaded_at"]))


def report(tag, result):
    dur = max(result["end"] - result["start"], 1e-9)
    print("[%s] %s  %s in %.2fs  (%.2f Mbps)" % (tag, result["name"], human(result["size"]), dur,
                                                 result["size"] * 8 / dur / 1e6))


def shell(client):
    print("commands: list | upload <path> [remote_name] | download <name> [out_dir] | quit")
    while True:
        try:
            parts = input("fs> ").split()
        except EOFError:
            break
        if not parts:
            continue
        cmd = parts[0].lower()
        try:
            if cmd == "list":
                print_files(client.list_files())
            elif cmd == "upload" and len(parts) >= 2:
                report("upload", client.upload(parts[1], parts[2] if len(parts) > 2 else None))
            elif cmd == "download" and len(parts) >= 2:
                report("download", client.download(parts[1], parts[2] if len(parts) > 2 else "downloads"))
            elif cmd in ("quit", "exit"):
                break
            else:
                print("unknown command")
        except (RuntimeError, OSError) as e:
            print("error:", e)


def main():
    p = argparse.ArgumentParser(description="TCP file-sharing client")
    p.add_argument("--host", default=SERVER_IP)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the TCP connection")
    p.add_argument("--id", default="c1", help="client id written to the results log")
    p.add_argument("--tag", default="manual", help="scenario label written to the results log")
    p.add_argument("--log", default="", help="CSV file to append transfer results to")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    up = sub.add_parser("upload")
    up.add_argument("path")
    up.add_argument("--as", dest="remote_name", default=None)
    down = sub.add_parser("download")
    down.add_argument("name")
    down.add_argument("--out", default="downloads")
    sub.add_parser("shell")
    args = p.parse_args()

    try:
        client = FileClient(args.host, args.port, args.timeout)
    except OSError as e:
        print("[%s] could not connect to %s:%d (%s) - blocked by network policy or server down"
              % (args.id, args.host, args.port, e))
        return 2

    try:
        if not client.login(args.user, args.password).get("ok"):
            print("[%s] login rejected for user %r" % (args.id, args.user))
            return 3
        if args.command == "list":
            print_files(client.list_files())
        elif args.command == "upload":
            name = args.remote_name or os.path.basename(args.path)
            try:
                result = client.upload(args.path, args.remote_name)
                report(args.id, result)
                log_result(args.log, make_row(args, "upload", name, result))
            except (RuntimeError, OSError) as e:
                print("[%s] upload failed: %s" % (args.id, e))
                log_result(args.log, make_row(args, "upload", name, error=str(e)))
                return 1
        elif args.command == "download":
            try:
                result = client.download(args.name, args.out)
                report(args.id, result)
                log_result(args.log, make_row(args, "download", args.name, result))
            except (RuntimeError, OSError) as e:
                print("[%s] download failed: %s" % (args.id, e))
                log_result(args.log, make_row(args, "download", args.name, error=str(e)))
                return 1
        elif args.command == "shell":
            shell(client)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
