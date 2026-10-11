#!/usr/bin/env python
"""Print a markdown table of the summary.json files under a runs directory.

    python table.py runs --container tuwunel

Container columns need runs made with load.py --docker-stats-prefix.
"""

import argparse
import glob
import json
import os


def ms(v):
    return "–" if v is None else f"{v * 1000:.0f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", help="directory holding one directory per run")
    ap.add_argument(
        "--container",
        action="append",
        default=[],
        help="add CPU and memory columns for this container; repeatable",
    )
    args = ap.parse_args()

    header = [
        "Run",
        "Users",
        "Msg/s",
        "Deliveries",
        "p50 ms",
        "p95 ms",
        "p99 ms",
        "Sync errors / timeouts",
        "Ready",
    ]
    for name in args.container:
        header += [f"{name} cores avg / max", f"{name} MiB"]
    header.append("Pass")
    print("| " + " | ".join(header) + " |")
    print("| --- " * len(header) + "|")

    for path in sorted(glob.glob(os.path.join(args.runs, "*", "summary.json"))):
        with open(path) as f:
            s = json.load(f)
        row = [
            os.path.basename(os.path.dirname(path)),
            s["users"],
            f"{s['msg_rate_per_s']:.2f}",
            s["deliveries"],
            ms(s["p50_s"]),
            ms(s["p95_s"]),
            ms(s["p99_s"]),
            f"{s['sync_non200'] + s['sync_errors']} / {s['sync_timeouts']}",
            s["ready"],
        ]
        for name in args.container:
            d = s["docker"].get(name)
            if d:
                row += [
                    f"{d['cpu_avg_pct'] / 100:.2f} / {d['cpu_max_pct'] / 100:.2f}",
                    d["mem_max_mib"],
                ]
            else:
                row += ["–", "–"]
        row.append("yes" if s["pass"] else "no")
        print("| " + " | ".join(str(x) for x in row) + " |")


if __name__ == "__main__":
    main()
