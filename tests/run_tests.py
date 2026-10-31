#!/usr/bin/env python3
"""Automated evaluation. Ryu controller must already be running. Then, from the project folder:

    sudo mn -c
    sudo python3 tests/run_tests.py                 # defaults: 20 Mbps, files 1/5/20 MB, 1/2/4/8 clients

Produces in results/:
    results.csv       one row per transfer (written by each client)
    iperf.txt         raw TCP capacity baseline
    access_test.txt   evidence for authentication + SDN blocking
    server.log        what the server saw
(bandwidth.csv / flows.csv are written by the controller at the same time)
Then run:  sudo python3 tests/plot.py
"""
import argparse
import os
import sys
import time
from subprocess import STDOUT

PROJECT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PROJECT, "sdn"))

from mininet.log import setLogLevel  # noqa: E402
from topology import build_net, start_server, stop_server  # noqa: E402

CLIENT = os.path.join(PROJECT, "app", "client.py")
RESULTS = os.path.join(PROJECT, "results")
RESULTS_CSV = os.path.join(RESULTS, "results.csv")
TEST_FILES = os.path.join(PROJECT, "test_files")
DOWNLOADS = os.path.join(PROJECT, "downloads")
USERS = {"h2": ("alice", "alice123"), "h3": ("bob", "bob123"), "h4": ("carol", "carol123")}
MB = 1024 * 1024


def make_file(name, size_mb):
    os.makedirs(TEST_FILES, exist_ok=True)
    path = os.path.join(TEST_FILES, name)
    if not os.path.exists(path) or os.path.getsize(path) != size_mb * MB:
        with open(path, "wb") as f:
            for _ in range(size_mb):
                f.write(os.urandom(MB))
    return path


def client_cmd(host, cid, tag, *op):
    user, pw = USERS[host.name]
    return ["python3", CLIENT, "--user", user, "--password", pw,
            "--id", cid, "--tag", tag, "--log", RESULTS_CSV] + list(op)


def run_parallel(jobs):
    """Start every (host, cmd) at the same moment, then wait for all. Returns number that succeeded."""
    procs = [(h, h.popen(cmd, stderr=STDOUT)) for h, cmd in jobs]
    ok = 0
    for h, p in procs:
        out, _ = p.communicate()
        if p.returncode == 0:
            ok += 1
        else:
            print("     ! %s failed: %s" % (h.name, out.decode(errors="replace").strip()))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bw", type=float, default=20)
    ap.add_argument("--sizes", default="1,5,20", help="file sizes in MB")
    ap.add_argument("--clients", default="1,2,4,8", help="numbers of simultaneous clients")
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]
    counts = [int(x) for x in args.clients.split(",")]

    setLogLevel("warning")
    os.makedirs(RESULTS, exist_ok=True)
    if os.path.exists(RESULTS_CSV):
        os.remove(RESULTS_CSV)

    net = build_net(bw=args.bw)
    net.start()
    try:
        start_server(net, fresh=True)
        h1, h2, h3, h4, h5 = (net.get("h%d" % i) for i in range(1, 6))
        workers = [h2, h3, h4]

        print("[1/5] connectivity (pingall)")
        loss = net.pingAll()
        print("      packet loss: %s%%" % loss)

        print("[2/5] iperf baseline h2 -> h1")
        bw = net.iperf((h2, h1), seconds=5)
        with open(os.path.join(RESULTS, "iperf.txt"), "w") as f:
            f.write("link setting: %s Mbps\niperf server/client: %s\n" % (args.bw, bw))
        print("      %s" % bw)

        print("[3/5] access control")
        allowed = h2.cmd("python3 %s --user alice --password alice123 list; echo EXIT=$?" % CLIENT)
        wrong_pw = h2.cmd("python3 %s --user alice --password WRONG list; echo EXIT=$?" % CLIENT)
        blocked = h5.cmd("python3 %s --user eve --password eve123 --timeout 5 list; echo EXIT=$?" % CLIENT)
        h5_ping = h5.cmd("ping -c 2 -W 1 10.0.0.1")
        with open(os.path.join(RESULTS, "access_test.txt"), "w") as f:
            f.write("== h2 alice, correct password (expect EXIT=0) ==\n" + allowed +
                    "\n== h2 alice, wrong password (expect EXIT=3: rejected by server login) ==\n" + wrong_pw +
                    "\n== h5 eve, correct password (expect EXIT=2: dropped by SDN rule) ==\n" + blocked +
                    "\n== h5 ping h1 (expect success: only the file service is blocked) ==\n" + h5_ping)
        print("      allowed user   : %s" % ("PASS" if "EXIT=0" in allowed else "FAIL"))
        print("      wrong password : %s" % ("PASS" if "EXIT=3" in wrong_pw else "FAIL"))
        print("      blocked host h5: %s" % ("PASS" if "EXIT=2" in blocked else "FAIL"))

        print("[4/5] concurrent transfers")
        for size in sizes:
            path = make_file("test_%dMB.bin" % size, size)
            for n in counts:
                names = ["s%d_n%d_c%d.bin" % (size, n, i + 1) for i in range(n)]
                ups = [(workers[i % 3], client_cmd(workers[i % 3], "c%d" % (i + 1), "upload_n%d" % n,
                                                   "upload", path, "--as", names[i])) for i in range(n)]
                up_ok = run_parallel(ups)
                time.sleep(1)
                downs = [(workers[i % 3], client_cmd(workers[i % 3], "c%d" % (i + 1), "download_n%d" % n,
                                                     "download", names[i], "--out",
                                                     os.path.join(DOWNLOADS, "c%d" % (i + 1)))) for i in range(n)]
                down_ok = run_parallel(downs)
                print("      %3d MB x %d clients: uploads %d/%d ok, downloads %d/%d ok"
                      % (size, n, up_ok, n, down_ok, n))
                time.sleep(1)

        print("[5/5] three clients upload DIFFERENT data under the SAME name at once (locking test)")
        originals = [make_file("same_name_src%d.bin" % i, 5) for i in range(3)]
        run_parallel([(workers[i], client_cmd(workers[i], "c%d" % (i + 1), "same_name",
                                              "upload", originals[i], "--as", "shared.bin")) for i in range(3)])
        out = h2.cmd("python3 %s --user alice --password alice123 download shared.bin --out %s; echo EXIT=$?"
                     % (CLIENT, os.path.join(DOWNLOADS, "same_name")))
        got = os.path.join(DOWNLOADS, "same_name", "shared.bin")
        src = [open(o, "rb").read() for o in originals]
        intact = os.path.exists(got) and open(got, "rb").read() in src
        print("      final file is exactly one complete upload (not mixed): %s" % ("PASS" if intact else "FAIL"))
        print("      " + out.strip().replace("\n", "\n      "))
    finally:
        stop_server(net)
        net.stop()

    print("\nDone. Results are in results/. Now run:  sudo python3 tests/plot.py --bw %g" % args.bw)


if __name__ == "__main__":
    main()
