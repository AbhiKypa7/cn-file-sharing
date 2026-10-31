"""Ryu SDN controller (OpenFlow 1.3) for the Concurrent File Sharing System.

Run (inside the ryu-env):
    ryu-manager --ofp-tcp-listen-port 6633 sdn/controller.py

What it does
  1. Learning switch  - forwards normal traffic (ARP, ping, ...) like a regular switch.
  2. Access policy    - DROPS TCP traffic from blocked hosts to the file server port,
                        right inside the switch (packets never reach the server).
  3. File-transfer flows - every client <-> server file connection gets its own
                        flow entry, so the switch counts bytes per client.
  4. Monitoring       - every 2 s asks the switch for port + flow counters,
                        turns them into Mbps and writes results/bandwidth.csv
                        and results/flows.csv.

Flow table priorities (higher number wins):
    100  blocked host -> server:5000          DROP
     10  one specific client <-> server flow   forward (counted)
      5  any TCP to/from port 5000             send to controller (first packet only)
      1  learned MAC src/dst/port              forward
      0  table-miss                            send to controller
"""
import csv
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ethernet, ether_types, ipv4, packet, tcp
from ryu.ofproto import inet, ofproto_v1_3

SERVER_IP = "10.0.0.1"
FILE_PORT = 5000
BLOCKED_IPS = {"10.0.0.5"}          # h5: not allowed to use the file service
POLL_INTERVAL = 2                    # seconds between statistics requests

PRIO_TABLE_MISS = 0
PRIO_L2 = 1
PRIO_FILE_TO_CONTROLLER = 5
PRIO_FILE_FLOW = 10
PRIO_BLOCK = 100

RESULTS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results"))


def _mget(match, key):
    try:
        return match[key]
    except KeyError:
        return None


class FileShareController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FileShareController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}        # dpid -> {mac: port}
        self.datapaths = {}          # connected switches
        self.prev_counters = {}      # key -> (time, byte_count), for rate calculation
        self.prev_blocked = {}       # ip -> packets dropped so far
        os.makedirs(RESULTS_DIR, exist_ok=True)
        self.bw_file = open(os.path.join(RESULTS_DIR, "bandwidth.csv"), "w", newline="")
        self.bw_csv = csv.writer(self.bw_file)
        self.bw_csv.writerow(["timestamp", "dpid", "port", "rx_mbps", "tx_mbps"])
        self.flow_file = open(os.path.join(RESULTS_DIR, "flows.csv"), "w", newline="")
        self.flow_csv = csv.writer(self.flow_file)
        self.flow_csv.writerow(["timestamp", "client_ip", "direction", "total_bytes", "mbps"])
        self.monitor_thread = hub.spawn(self._monitor)

    # ------------------------------------------------------------------ helpers
    def _add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # No instructions at all = DROP (OpenFlow 1.3 rule).
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle_timeout))

    def _rate_mbps(self, key, now, byte_count):
        prev = self.prev_counters.get(key)
        self.prev_counters[key] = (now, byte_count)
        if prev is None or now <= prev[0]:
            return 0.0
        return max(0, byte_count - prev[1]) * 8 / (now - prev[0]) / 1e6

    # ------------------------------------------------------------------ switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        to_controller = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]

        # 0: anything unknown -> ask the controller
        self._add_flow(dp, PRIO_TABLE_MISS, parser.OFPMatch(), to_controller)

        # 100: ACCESS POLICY - blocked hosts can never open a TCP connection to the file server
        for ip in sorted(BLOCKED_IPS):
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=inet.IPPROTO_TCP,
                                    ipv4_src=ip, ipv4_dst=SERVER_IP, tcp_dst=FILE_PORT)
            self._add_flow(dp, PRIO_BLOCK, match, [])
            self.logger.info("[policy] switch %s: DROP %s -> %s:%d", dp.id, ip, SERVER_IP, FILE_PORT)

        # 5: first packet of any file-service connection comes to us, so we can give it its own flow
        for field in ("tcp_dst", "tcp_src"):
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=inet.IPPROTO_TCP,
                                    **{field: FILE_PORT})
            self._add_flow(dp, PRIO_FILE_TO_CONTROLLER, match, to_controller)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ------------------------------------------------------------------ packets sent to us
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Learn: "this MAC lives behind this port"
        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofp.OFPP_FLOOD:
            ip = pkt.get_protocol(ipv4.ipv4)
            seg = pkt.get_protocol(tcp.tcp)
            if ip and seg and FILE_PORT in (seg.src_port, seg.dst_port):
                if ip.src in BLOCKED_IPS and seg.dst_port == FILE_PORT:
                    return  # safety net; the priority-100 rule normally drops these in the switch
                if seg.dst_port == FILE_PORT:          # client -> server
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=inet.IPPROTO_TCP,
                                            ipv4_src=ip.src, ipv4_dst=ip.dst, tcp_dst=FILE_PORT)
                    label = "%s -> server" % ip.src
                else:                                   # server -> client
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=inet.IPPROTO_TCP,
                                            ipv4_src=ip.src, ipv4_dst=ip.dst, tcp_src=FILE_PORT)
                    label = "server -> %s" % ip.dst
                self._add_flow(dp, PRIO_FILE_FLOW, match, actions, idle_timeout=60)
                self.logger.info("[flow] file-transfer flow installed: %s", label)
            else:
                match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
                self._add_flow(dp, PRIO_L2, match, actions)

        # Forward this first packet ourselves (later packets are handled by the switch alone)
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
                                        actions=actions, data=msg.data))

    # ------------------------------------------------------------------ monitoring
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                dp.send_msg(parser.OFPFlowStatsRequest(dp))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        busy = []
        for st in sorted(ev.msg.body, key=lambda s: s.port_no):
            if st.port_no > dp.ofproto.OFPP_MAX:       # skip the switch's internal LOCAL port
                continue
            # rx = bytes the switch RECEIVED on this port (host sending); tx = switch SENT (host receiving)
            rx = self._rate_mbps(("port", dp.id, st.port_no, "rx"), now, st.rx_bytes)
            tx = self._rate_mbps(("port", dp.id, st.port_no, "tx"), now, st.tx_bytes)
            self.bw_csv.writerow(["%.3f" % now, dp.id, st.port_no, "%.3f" % rx, "%.3f" % tx])
            if rx + tx > 0.1:
                busy.append("port %d: in %.2f / out %.2f Mbps" % (st.port_no, rx, tx))
        self.bw_file.flush()
        if busy:
            self.logger.info("[bandwidth] %s", " | ".join(busy))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        for st in ev.msg.body:
            m = st.match
            if st.priority == PRIO_FILE_FLOW:
                if _mget(m, "tcp_dst") == FILE_PORT:
                    client, direction = _mget(m, "ipv4_src"), "to_server"
                else:
                    client, direction = _mget(m, "ipv4_dst"), "from_server"
                mbps = self._rate_mbps(("flow", dp.id, client, direction), now, st.byte_count)
                self.flow_csv.writerow(["%.3f" % now, client, direction, st.byte_count, "%.3f" % mbps])
            elif st.priority == PRIO_BLOCK:
                ip = _mget(m, "ipv4_src")
                if st.packet_count != self.prev_blocked.get(ip, 0):
                    self.prev_blocked[ip] = st.packet_count
                    self.flow_csv.writerow(["%.3f" % now, ip, "blocked", st.byte_count, "0"])
                    self.logger.info("[policy] %d packets from %s dropped so far", st.packet_count, ip)
        self.flow_file.flush()
