"""User accounts + file metadata, kept in a small SQLite database.

Command line (run from the project folder):
    python3 app/storage.py init                  # create the demo users
    python3 app/storage.py adduser <user> <pw>   # add / reset a user
    python3 app/storage.py users                 # list usernames
    python3 app/storage.py files                 # list stored file metadata

Passwords are NEVER stored. We store a random salt + PBKDF2-SHA256 hash.
Login = hash the typed password with the same salt and compare.
"""
import hashlib
import hmac
import os
import sqlite3
import sys
import threading
from datetime import datetime

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_ROOT = os.path.join(PROJECT_DIR, "server_storage")
DEFAULT_DB = os.path.join(DEFAULT_ROOT, "meta.db")
PBKDF2_ITERATIONS = 200_000

# h2 -> alice, h3 -> bob, h4 -> carol, h5 -> eve (eve has a VALID password but is blocked by SDN)
DEMO_USERS = {"alice": "alice123", "bob": "bob123", "carol": "carol123", "eve": "eve123"}


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)


class Storage:
    def __init__(self, db_path=DEFAULT_DB):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # One connection shared by all server threads, protected by a lock.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                " username TEXT PRIMARY KEY, salt BLOB NOT NULL, pw_hash BLOB NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                " name TEXT PRIMARY KEY, size INTEGER NOT NULL, owner TEXT NOT NULL,"
                " sha256 TEXT NOT NULL, uploaded_at TEXT NOT NULL)")

    # ---------- users ----------
    def add_user(self, username, password):
        salt = os.urandom(16)
        pw_hash = _hash_password(password, salt)
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?)", (username, salt, pw_hash))

    def check_login(self, username, password):
        with self._lock:
            row = self._db.execute(
                "SELECT salt, pw_hash FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            _hash_password(password, b"\0" * 16)  # same amount of work, so timing doesn't reveal valid usernames
            return False
        salt, stored = row
        # Hashing happens OUTSIDE the lock so slow logins don't block other threads.
        return hmac.compare_digest(_hash_password(password, salt), stored)

    def list_users(self):
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT username FROM users ORDER BY username")]

    # ---------- file metadata ----------
    def save_file_info(self, name, size, owner, sha256):
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)",
                             (name, size, owner, sha256, now))

    def get_file_info(self, name):
        with self._lock:
            row = self._db.execute(
                "SELECT name, size, owner, sha256, uploaded_at FROM files WHERE name = ?", (name,)).fetchone()
        return None if row is None else dict(zip(("name", "size", "owner", "sha256", "uploaded_at"), row))

    def list_files(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT name, size, owner, sha256, uploaded_at FROM files ORDER BY name").fetchall()
        return [dict(zip(("name", "size", "owner", "sha256", "uploaded_at"), r)) for r in rows]


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    st = Storage()
    cmd = argv[0]
    if cmd == "init":
        for user, pw in DEMO_USERS.items():
            st.add_user(user, pw)
        print("demo users ready:", ", ".join(DEMO_USERS))
    elif cmd == "adduser" and len(argv) == 3:
        st.add_user(argv[1], argv[2])
        print("user saved:", argv[1])
    elif cmd == "users":
        print("\n".join(st.list_users()) or "(no users)")
    elif cmd == "files":
        for f in st.list_files():
            print("%-30s %12d B  owner=%-8s %s  sha256=%s..." % (
                f["name"], f["size"], f["owner"], f["uploaded_at"], f["sha256"][:12]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
