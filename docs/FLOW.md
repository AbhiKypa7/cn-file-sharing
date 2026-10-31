# How the System Works (Viva Guide)

## 1. The big picture

There are four parts, and each one has one job:

| Part | Job | File |
|---|---|---|
| Mininet | Builds a fake network: 5 hosts + 1 switch, with 20 Mbps links | `sdn/topology.py` |
| SDN controller (Ryu) | The switch's brain: decides who may talk to whom, and measures traffic | `sdn/controller.py` |
| File server (h1) | Accepts TCP connections, checks logins, stores files + metadata | `app/server.py`, `app/storage.py` |
| Clients (h2–h5) | Connect, log in, upload / download / list | `app/client.py` |

`app/common.py` holds the protocol: the shared "language" both sides speak.

**The data plane vs the control plane.** The switch only forwards packets using its flow table (data plane). The controller fills that table (control plane). In SDN these are separated: the switch is "dumb", and the logic lives in software.

---

## 2. The life of one upload (memorise this, it's the main viva answer)

`h2 python3 app/client.py --user alice --password alice123 upload demo.bin`

1. **Fingerprint.** The client computes the SHA-256 of `demo.bin` and its size.
2. **TCP handshake.** `socket.create_connection(("10.0.0.1", 5000))` sends a SYN. The packet reaches switch s1.
3. **The switch asks the controller.** s1 has no specific rule for this connection, but it matches the priority-5 rule "TCP port 5000 → send to controller". The controller learns which port h2 and h1 are on, installs a priority-10 flow just for `10.0.0.2 → 10.0.0.1:5000`, and forwards the packet. The reply direction gets its own flow the same way. From now on the switch forwards on its own; the controller only reads its counters.
4. **Server accepts.** `accept()` returns a new socket for this client, and the server starts a new thread for it. The main thread goes straight back to waiting for the next client.
5. **Login.** Client sends `{"cmd":"LOGIN","user":"alice","pass":"alice123"}`. The server takes the stored salt, hashes the password with PBKDF2, and compares. If it matches, it sends `{"ok":true}`.
6. **Upload header.** Client sends `{"cmd":"UPLOAD","name":"demo.bin","size":10485760,"sha256":"..."}`.
7. **Server says ready.** The server checks the name and size, then replies `{"ok":true,"ready":true}`. If something was wrong, it says no *before* any data is sent, so no bandwidth is wasted.
8. **Data.** The client sends exactly 10,485,760 bytes in 64 KB chunks. The server reads exactly that many bytes into a temp file, hashing as it goes.
9. **Verify.** If the server's hash doesn't match the client's, the file is corrupted, so it's deleted and an error is sent.
10. **Commit.** Under that filename's lock, the temp file is moved into place (`os.replace`, which is atomic) and the metadata row is written (name, size, owner, hash, time).
11. **Confirm.** The server sends `{"ok":true}`. The client measures the time and logs the throughput. Then it sends QUIT and the connection closes (FIN handshake).

**Download is the mirror image:** request, then the server sends `{"ok":true,"size":N,"sha256":...}`, then N bytes, then the client verifies the hash before keeping the file.

---

## 3. The protocol

| Client sends | Server replies |
|---|---|
| `LOGIN user pass` | `ok` true/false |
| `LIST` | `ok` + list of file metadata |
| `UPLOAD name size sha256` | `ready`, then (after N bytes) `ok` + sha256 |
| `DOWNLOAD name` | `ok` + size + sha256, then N bytes |
| `QUIT` | `ok`, then close |

Any command before a successful LOGIN gets an error, and the connection is closed.

---

## 4. Design decisions and why (examiners love "why")

**Length-prefix framing (size before data).** TCP is a byte stream with no message boundaries. Two `send()` calls can arrive as one `recv()`, or one can arrive split into many. Telling the receiver the exact size is the only way it knows where the file ends and the next command begins.

**One thread per client.** `accept()` and `recv()` block. With one thread, client 2 would wait until client 1 finished. With a thread each, transfers overlap. The alternatives are `select`/asyncio (one thread, event-driven) or processes. Threads are simplest, and Python's GIL is released during network I/O, so threads work well for I/O-bound work like this.

**Temp file + per-file lock + atomic replace.** The slow part (receiving data) happens without any lock, so different uploads run in parallel. Only the quick final step (swap the file in and write the metadata) is locked, so two uploads of the same name can never mix their bytes. The test proves this: three clients upload different data as `shared.bin` at the same time, and the final file is exactly one of them. Downloads open the file under the lock, so the metadata and the file always match.

**Salted PBKDF2 hashes, not plain passwords.** If the database leaks, the passwords aren't exposed. The salt means two users with the same password have different hashes. PBKDF2 is deliberately slow (200,000 rounds), which makes guessing expensive. `hmac.compare_digest` avoids timing attacks.

**SHA-256 checksums.** These give end-to-end integrity: they prove the file on the other side is bit-for-bit identical.

**`safe_name()`.** Strips folder paths, so a client can't upload to `../../etc/passwd` (path traversal).

---

## 5. The SDN part

**Flow table (higher priority wins):**

| Priority | Match | Action | Why |
|---|---|---|---|
| 100 | TCP from 10.0.0.5 to 10.0.0.1:5000 | DROP | Access policy |
| 10 | One specific client ↔ server:5000 | Forward | Per-client counting |
| 5 | Any TCP to/from port 5000 | To controller | So each new client gets its own flow |
| 1 | Learned MAC src/dst/port | Forward | Normal switching |
| 0 | Anything | To controller | Table-miss |

**Access policy.** The drop rule is installed proactively, the moment the switch connects. h5's SYN packets are dropped inside the switch and never reach the server or the controller. h5 never completes the TCP handshake, so its client times out (exit code 2). Ping still works, which shows the policy is targeted: only the file service is blocked.

**Two layers of security.** The network layer (SDN) decides which *hosts* may reach the service. The application layer (login) decides which *users* may use it. eve has a valid password but is still blocked. alice on an allowed host with the wrong password is rejected by the server.

**Bandwidth monitoring.** Every 2 seconds the controller sends `PortStatsRequest` and `FlowStatsRequest`. The switch replies with byte counters. Rate = (bytes now − bytes before) × 8 ÷ seconds, which gives Mbps. Port stats give per-host traffic (port N = hN). Flow stats give file-transfer traffic per client, which is possible only because each client got its own priority-10 flow.

**Reactive vs proactive rules.** The drop rule is proactive: installed before any traffic. The per-client flows are reactive: installed when the first packet arrives.

---

## 6. Expected results and how to explain them

- **iperf baseline** is about 19 Mbps on a 20 Mbps link. That's the ceiling.
- **One client** gets close to the iperf number. Our protocol adds very little overhead (just a JSON line per file).
- **More clients:** per-client throughput drops to about capacity ÷ N (2 clients get about 9.5 each, 4 get about 4.8). The aggregate stays near link capacity. **Why:** all clients share the server's single 20 Mbps link, which is the bottleneck. TCP congestion control (AIMD) makes competing connections converge to roughly equal shares. This is **TCP fairness**.
- **Completion time** grows roughly linearly with clients × file size, because the same link is carrying N times the data.
- **Small files (1 MB)** show lower throughput than big files. The TCP handshake, slow start and login are a fixed cost that doesn't get spread over much data.

---

## 7. Likely viva questions

1. **Why TCP and not UDP?** Files must arrive complete and in order. TCP gives reliability, ordering, retransmission and congestion control for free. With UDP we'd have to rebuild all of that ourselves.
2. **Why send the size first?** TCP has no message boundaries (section 4).
3. **What happens if a client disconnects mid-upload?** `recv` returns empty, `recv_to_file` raises an error, the temp file is deleted, and the old version of the file and its metadata stay untouched.
4. **What if two clients upload the same file at once?** Both receive into separate temp files in parallel. The per-file lock makes the final swap and metadata write happen one at a time, so the result is one complete version, never a mix.
5. **What does `listen(64)` mean?** The backlog: how many connections can wait to be accepted.
6. **What is `SO_REUSEADDR`?** It lets the server restart immediately, without waiting for old connections in TIME_WAIT to expire.
7. **Where does the 3-way handshake happen in your code?** On the client, in `create_connection()`. On the server, inside the kernel before `accept()` returns.
8. **How does the controller block h5?** A priority-100 flow with no actions (which means drop) matching src 10.0.0.5, dst 10.0.0.1, TCP dst 5000.
9. **Why does h5's client wait about 5 s instead of failing instantly?** The SYN is silently dropped, so there's no RST. The client keeps retransmitting until its 5 s connect timeout expires. (A closed port fails instantly, because the host sends back a RST.)
10. **How is bandwidth measured?** Byte counters from port/flow stats every 2 s: delta bytes × 8 ÷ delta time.
11. **What's the controller's role once a flow is installed?** Almost none. The switch forwards at line rate, and the controller only polls statistics. That's the point of SDN.
12. **How would you scale this?** Multiple servers with load balancing done by the controller, `asyncio` instead of threads, TLS for encryption, and resumable transfers using byte offsets.
13. **Weaknesses?** Passwords cross the network in plain text (fix with TLS). The thread-per-client model doesn't scale to thousands of clients. A single server is a single point of failure.
