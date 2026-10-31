#!/usr/bin/env python3
"""Turn the CSV results into graphs + a summary table for the report.

    sudo python3 tests/plot.py --bw 20        # --bw = link bandwidth used in the tests

Needs matplotlib:  sudo apt install -y python3-matplotlib
Output: results/plots/*.png  and  results/summary.csv
"""
import argparse
import csv
import os
from collections import defaultdict
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PROJECT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RESULTS = os.path.join(PROJECT, "results")
PLOTS = os.path.join(RESULTS, "plots")
OPS = ("upload", "download")


def summarise():
    groups = defaultdict(list)                     # (op, size_mb, n_clients) -> rows
    with open(os.path.join(RESULTS, "results.csv")) as f:
        for r in csv.DictReader(f):
            tag = r["tag"]
            if r["ok"] != "1" or not tag.startswith(("upload_n", "download_n")):
                continue
            op, n = tag.split("_n")
            groups[(op, round(int(r["size_bytes"]) / 1048576), int(n))].append(r)

    rows = []
    for (op, size, n), rs in sorted(groups.items()):
        start = min(float(r["start_ts"]) for r in rs)
        end = max(float(r["end_ts"]) for r in rs)
        total_bits = sum(int(r["size_bytes"]) for r in rs) * 8
        rows.append({
            "op": op, "size_mb": size, "clients": n, "completed": len(rs),
            "avg_per_client_mbps": round(mean(float(r["throughput_mbps"]) for r in rs), 3),
            "aggregate_mbps": round(total_bits / max(end - start, 1e-9) / 1e6, 3),
            "avg_completion_s": round(mean(float(r["duration_s"]) for r in rs), 3),
            "max_completion_s": round(max(float(r["duration_s"]) for r in rs), 3),
        })
    with open(os.path.join(RESULTS, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def plot_metric(rows, metric, ylabel, title, filename, capacity=None):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, op in zip(axes, OPS):
        for size in sorted({r["size_mb"] for r in rows}):
            pts = sorted((r["clients"], r[metric]) for r in rows if r["op"] == op and r["size_mb"] == size)
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label="%d MB files" % size)
        if capacity:
            ax.axhline(capacity, linestyle="--", color="gray", label="link capacity (%g Mbps)" % capacity)
        ax.set_title(op.capitalize())
        ax.set_xlabel("simultaneous clients")
        ax.set_xticks(sorted({r["clients"] for r in rows}))
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    axes[0].set_ylabel(ylabel)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, filename), dpi=150)
    plt.close(fig)


def plot_bandwidth():
    path = os.path.join(RESULTS, "bandwidth.csv")
    if not os.path.exists(path):
        return False
    series = defaultdict(list)
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return False
    t0 = float(rows[0]["timestamp"])
    for r in rows:
        series[int(r["port"])].append((float(r["timestamp"]) - t0, float(r["rx_mbps"]) + float(r["tx_mbps"])))
    fig, ax = plt.subplots(figsize=(11, 4))
    for port in sorted(series):
        label = "port %d - h%d%s" % (port, port, " (server)" if port == 1 else "")
        ax.plot([p[0] for p in series[port]], [p[1] for p in series[port]], label=label, linewidth=1)
    ax.set_xlabel("time since controller start (s)")
    ax.set_ylabel("Mbps (in + out)")
    ax.set_title("Per-port bandwidth measured by the SDN controller")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "sdn_port_bandwidth.png"), dpi=150)
    plt.close(fig)
    return True


def plot_flows():
    path = os.path.join(RESULTS, "flows.csv")
    if not os.path.exists(path):
        return False
    series = defaultdict(list)
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r["direction"] != "blocked"]
    if not rows:
        return False
    t0 = float(rows[0]["timestamp"])
    for r in rows:
        series[(r["client_ip"], r["direction"])].append((float(r["timestamp"]) - t0, float(r["mbps"])))
    fig, ax = plt.subplots(figsize=(11, 4))
    for (ip, direction), pts in sorted(series.items()):
        ax.plot([p[0] for p in pts], [p[1] for p in pts], label="%s %s" % (ip, direction), linewidth=1)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("Mbps")
    ax.set_title("File-transfer traffic per client (SDN flow statistics)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "sdn_flow_bandwidth.png"), dpi=150)
    plt.close(fig)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bw", type=float, default=20, help="link bandwidth used in the tests (Mbps)")
    args = ap.parse_args()
    os.makedirs(PLOTS, exist_ok=True)

    rows = summarise()
    plot_metric(rows, "avg_per_client_mbps", "Mbps per client",
                "Average throughput per client vs number of clients", "per_client_throughput.png")
    plot_metric(rows, "aggregate_mbps", "Mbps (all clients together)",
                "Aggregate throughput vs number of clients", "aggregate_throughput.png", capacity=args.bw)
    plot_metric(rows, "avg_completion_s", "seconds",
                "Average transfer completion time vs number of clients", "completion_time.png")
    plot_bandwidth()
    plot_flows()

    print("%-9s %6s %8s %14s %12s %12s" % ("op", "size", "clients", "per-client", "aggregate", "avg time"))
    for r in rows:
        print("%-9s %4dMB %8d %10.2f Mbps %7.2f Mbps %10.2fs" % (
            r["op"], r["size_mb"], r["clients"], r["avg_per_client_mbps"], r["aggregate_mbps"],
            r["avg_completion_s"]))
    print("\nGraphs saved in results/plots/, table in results/summary.csv")


if __name__ == "__main__":
    main()
