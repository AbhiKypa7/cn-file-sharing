#!/usr/bin/env python3
"""Mininet network for the project. Start the Ryu controller FIRST, then:

    sudo python3 sdn/topology.py            # 20 Mbps links, opens the Mininet CLI

            h2 (alice) ─┐
            h3 (bob)   ─┤
            h4 (carol) ─┼── s1 (Open vSwitch, OpenFlow 1.3) ── h1 10.0.0.1 (file server :5000)
            h5 (eve)   ─┘            │
                               Ryu controller (127.0.0.1:6633)

Host hN gets IP 10.0.0.N and sits on switch port N (s1-ethN).
"""
import argparse
import os
import shutil
import time

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSKernelSwitch, RemoteController

PROJECT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
APP = os.path.join(PROJECT, "app")
RESULTS = os.path.join(PROJECT, "results")


def build_net(bw=20, delay="2ms", ctrl_ip="127.0.0.1", ctrl_port=6633):
    net = Mininet(controller=None, switch=OVSKernelSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=ctrl_ip, port=ctrl_port)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    for i in range(1, 6):
        h = net.addHost("h%d" % i, ip="10.0.0.%d/24" % i)
        net.addLink(h, s1, bw=bw, delay=delay)   # TCLink: real bandwidth limit, so results mean something
    return net


def start_server(net, fresh=False):
    h1 = net.get("h1")
    os.makedirs(RESULTS, exist_ok=True)
    if fresh:
        shutil.rmtree(os.path.join(PROJECT, "server_storage"), ignore_errors=True)
    h1.cmd("python3 %s/storage.py init" % APP)
    h1.cmd("python3 -u %s/server.py > %s/server.log 2>&1 &" % (APP, RESULTS))
    for _ in range(30):                      # wait until the server is actually listening
        if ":5000" in h1.cmd("ss -ltn"):
            info("*** file server running on h1 (10.0.0.1:5000), log: results/server.log\n")
            return
        time.sleep(0.2)
    info("*** WARNING: server did not start, check results/server.log\n")


def stop_server(net):
    net.get("h1").cmd("pkill -f app/server.py")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bw", type=float, default=20, help="link bandwidth in Mbps")
    p.add_argument("--delay", default="2ms")
    p.add_argument("--fresh", action="store_true", help="delete stored files/metadata before starting")
    args = p.parse_args()

    setLogLevel("info")
    net = build_net(args.bw, args.delay)
    net.start()
    try:
        start_server(net, fresh=args.fresh)
        info("\n*** Try:\n"
             "mininet> h2 python3 app/client.py --user alice --password alice123 list\n"
             "mininet> h5 python3 app/client.py --user eve --password eve123 list   (blocked by SDN)\n"
             "mininet> xterm h2 h3 h4                                               (parallel demo)\n\n")
        CLI(net)
    finally:
        stop_server(net)
        net.stop()


if __name__ == "__main__":
    main()
