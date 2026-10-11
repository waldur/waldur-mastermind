#!/usr/bin/env python
"""Run load.py once per user count, one after the other, and print a line per step.

Every step uses the first N sessions of the same file, so provision the largest
count first. Arguments after `--` go to every load.py run.

    uv run --with httpx python steps.py --sessions sessions.jsonl \\
        --base-url http://127.0.0.1:6167 --out-dir runs 250 500 1000 \\
        -- --docker-stats-prefix tuwunel

Each step writes <out-dir>/<tag>n<users>/: load.py's summary.json and
part-*.json, and its output in load.log. See README.md next to this file.
"""

import argparse
import json
import os
import subprocess
import sys

LOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load.py")


def main():
    argv = sys.argv[1:]
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(
        description="Run load.py once per user count.",
        epilog="Arguments after -- are passed to every load.py run.",
    )
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument(
        "--tag", default="", help="prefix for the step directories, e.g. cpu4-"
    )
    ap.add_argument("users", type=int, nargs="+", help="user counts, in order")
    args = ap.parse_args(argv)

    for n in args.users:
        out = os.path.join(args.out_dir, f"{args.tag}n{n:05d}")
        os.makedirs(out, exist_ok=True)
        cmd = [
            sys.executable,
            LOAD,
            "--sessions",
            args.sessions,
            "--users",
            str(n),
            "--base-url",
            args.base_url,
            "--out",
            out,
            *extra,
        ]
        with open(os.path.join(out, "load.log"), "w") as log:
            code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        path = os.path.join(out, "summary.json")
        if code or not os.path.exists(path):
            sys.exit(f"step {n} failed, see {os.path.join(out, 'load.log')}")
        with open(path) as f:
            s = json.load(f)
        print(
            json.dumps(
                {
                    "users": n,
                    "pass": s["pass"],
                    "ready": s["ready"],
                    "p50_s": s["p50_s"],
                    "p95_s": s["p95_s"],
                    "p99_s": s["p99_s"],
                    "sync_timeouts": s["sync_timeouts"],
                    "sync_failures": s["sync_non200"] + s["sync_errors"],
                    "sends_failed": s["sends_failed"],
                    "refresh_failed": s["refresh_failed"],
                    "loop_lag_p99_s": s["loop_lag_p99_s"],
                    "window": [s["window_start"], s["window_end"]],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
