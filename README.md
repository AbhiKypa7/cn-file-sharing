# Concurrent File Sharing System (TCP + SDN)

Multi-user file sharing over TCP sockets. Clients log in, then upload and download files concurrently. A Ryu SDN controller blocks unauthorised hosts at the switch and monitors file-transfer bandwidth.

```
 h2 alice ─┐
 h3 bob   ─┤
 h4 carol ─┼── s1 (OpenFlow 1.3) ── h1 10.0.0.1  file server :5000
 h5 eve   ─┘        │
           Ryu controller (access policy + monitoring)
```

| Folder | Contents |
|---|---|
| `app/` | `server.py`, `client.py`, `common.py` (protocol), `storage.py` (users + metadata) |
| `sdn/` | `controller.py` (Ryu app), `topology.py` (Mininet network) |
| `tests/` | `run_tests.py` (automated evaluation), `plot.py` (graphs) |
| `docs/` | `FLOW.md` (how everything works, plus viva questions) |

**Demo users:** alice/alice123 (h2), bob/bob123 (h3), carol/carol123 (h4), eve/eve123 (h5; valid password, but the host is blocked by SDN).

---

## One-time setup (inside the Ubuntu VM)

Mininet and Ryu must already be installed (see the setup message). Then:

```bash
sudo apt install -y python3-matplotlib git
git clone <repo link>
cd cn-file-sharing
mkdir -p results          # must exist BEFORE the controller starts
```

To get the latest code later: `cd cn-file-sharing && git pull`

**Always run every command from inside the `cn-file-sharing` folder.**

---

## 1. Live demo (manual)

**Terminal 1: controller**
```bash
cd cn-file-sharing
source ~/ryu-env/bin/activate
ryu-manager --ofp-tcp-listen-port 6633 sdn/controller.py
```
Leave it running. It prints policy, flow and bandwidth messages.

**Terminal 2: network + server**
```bash
cd cn-file-sharing
sudo mn -c
sudo python3 sdn/topology.py --fresh
```
This starts the network and the file server on h1. You'll land at the `mininet>` prompt.

**Demo commands at `mininet>`:**
```
pingall
h2 dd if=/dev/urandom of=demo.bin bs=1M count=10
h2 python3 app/client.py --user alice --password alice123 upload demo.bin
h3 python3 app/client.py --user bob --password bob123 list
h3 python3 app/client.py --user bob --password bob123 download demo.bin --out downloads/bob
h2 python3 app/client.py --user alice --password WRONG list
h5 python3 app/client.py --user eve --password eve123 list
h5 ping -c 2 10.0.0.1
```

| Command | Expected result |
|---|---|
| wrong password | `login rejected` (server-side authentication) |
| h5 list | `could not connect` after about 5 s (SDN drops h5's packets) |
| h5 ping | works (only the file service is blocked, not the whole host) |

**Parallel demo:** run `xterm h2 h3 h4`, and in each window start the interactive client:
```
python3 app/client.py --user alice --password alice123 shell
```
(use bob or carol in the other windows). Upload in all three at the same time. Terminal 1 shows the per-port bandwidth live.

Type `exit` to leave Mininet.

---

## 2. Automated tests + graphs (for the report)

Controller running in Terminal 1 (restart it first for clean bandwidth logs). Then in Terminal 2:
```bash
sudo mn -c
sudo python3 tests/run_tests.py
sudo python3 tests/plot.py --bw 20
```
The tests take about 10 minutes. Every line should say PASS / ok.

Output in `results/`:

| File | What it is |
|---|---|
| `plots/per_client_throughput.png` | throughput per client as clients increase |
| `plots/aggregate_throughput.png` | total throughput vs link capacity |
| `plots/completion_time.png` | transfer time vs number of clients |
| `plots/sdn_port_bandwidth.png` | bandwidth seen by the controller per switch port |
| `plots/sdn_flow_bandwidth.png` | file-transfer traffic per client from flow stats |
| `summary.csv` | the numbers behind the graphs |
| `access_test.txt` | evidence: allowed / wrong password / blocked host |
| `iperf.txt` | raw link capacity baseline |
| `server.log` | every connection, login, upload and download |

---

## 3. Wireshark captures

With the demo network running (section 1), in a third terminal: `sudo wireshark &`

| Capture on | Display filter | Do this | Screenshot |
|---|---|---|---|
| `s1-eth1` (server port) | `tcp.port == 5000` | h2 upload | SYN, SYN-ACK, ACK handshake, then the JSON header and data packets |
| `s1-eth5` (h5's port) | `tcp.port == 5000` | h5 list | repeated SYN retransmissions with no reply (dropped by the switch) |
| `s1-eth1` | `tcp.port == 5000` | `h2 python3 app/client.py --user alice --password WRONG list` | login JSON and the rejection |

In Wireshark, right-click a packet, then **Follow → TCP Stream**, to show the readable JSON commands.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Anything weird after a crash | `sudo mn -c`, restart controller, restart topology |
| `pingall` fails | controller not running, or not started with `--ofp-tcp-listen-port 6633` |
| Controller crashes with `Permission denied: results/...` | `sudo chown -R $USER results` |
| Server didn't start | check `results/server.log` |
| `ModuleNotFoundError: mininet` | run topology/tests with system `python3` (not inside ryu-env) and with `sudo` |
| `ModuleNotFoundError: matplotlib` | `sudo apt install -y python3-matplotlib` |

When reporting an error, send the **full** error text and say which command produced it.
