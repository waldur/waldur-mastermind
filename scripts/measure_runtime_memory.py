#!/usr/bin/env python
"""Measure runtime (multi-worker) memory of a Waldur image inside Linux.

Runs the gunicorn (or celery) process tree from a built image in a throwaway
container, lets the workers boot and import every app, then samples:

* the container cgroup ``memory.current`` -- the number Kubernetes limits and the
  OOM killer act on; copy-on-write shared pages are counted once, so this reveals
  the gunicorn ``--preload`` win and the jemalloc footprint difference,
* the summed PSS / RSS of the worker tree as a cross-check (PSS << sum(RSS) is the
  signature of copy-on-write page sharing).

Boots under ``base_settings`` so no database / secrets are required -- we only
need the import footprint resident, not live request serving.

Results append to ``memory_profiles/runtime_summary.csv`` keyed by ``--label``.

Usage:
    uv run python scripts/measure_runtime_memory.py --label baseline \
        --image waldur-mem:baseline
"""

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

OUTDIR = Path(__file__).resolve().parent / "memory_profiles"
RUNTIME_CSV = OUTDIR / "runtime_summary.csv"
SETTINGS = "waldur_core.server.base_settings"

# Sampler runs inside the container: walk /proc, sum Pss/Rss for the matched
# process tree, and read the cgroup-v2 current usage. Emits one JSON line.
SAMPLER = r"""
import json, os, glob
match = os.environ.get("MEM_MATCH", "gunicorn")
procs = []
for p in glob.glob("/proc/[0-9]*"):
    pid = p.split("/")[-1]
    try:
        cmd = open(p + "/cmdline", "rb").read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        continue
    if match not in cmd:
        continue
    pss = rss = 0
    try:
        for line in open(p + "/smaps_rollup"):
            if line.startswith("Pss:"):
                pss = int(line.split()[1])
            elif line.startswith("Rss:"):
                rss = int(line.split()[1])
    except OSError:
        continue
    procs.append({"pid": pid, "pss_kb": pss, "rss_kb": rss})
cur = 0
for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
    try:
        cur = int(open(path).read().strip()); break
    except OSError:
        pass
print(json.dumps({
    "nproc": len(procs),
    "sum_pss_mb": round(sum(x["pss_kb"] for x in procs) / 1024, 1),
    "sum_rss_mb": round(sum(x["rss_kb"] for x in procs) / 1024, 1),
    "cgroup_current_mb": round(cur / 1024 / 1024, 1),
}))
"""


def docker(*args, **kw):
    return subprocess.run(["docker", *args], text=True, capture_output=True, **kw)


def start_container(
    name: str, image: str, mode: str, preload: str | None, ld_preload: str | None
) -> None:
    env = ["-e", f"DJANGO_SETTINGS_MODULE={SETTINGS}"]
    if preload is not None:
        env += ["-e", f"GUNICORN_PRELOAD={preload}"]
    if ld_preload is not None:
        # Empty string overrides the image ENV to disable jemalloc.
        env += ["-e", f"LD_PRELOAD={ld_preload}"]
    if mode == "api":
        env += ["-e", "MEM_MATCH=gunicorn"]
        cmd = [
            "--entrypoint",
            "gunicorn",
            image,
            "-c",
            "/etc/waldur/gunicorn.conf.py",
            "waldur_core.server.wsgi:application",
        ]
    else:  # celery
        env += ["-e", "MEM_MATCH=celery", "-e", "C_FORCE_ROOT=1"]
        cmd = [
            "--entrypoint",
            "celery",
            image,
            "-A",
            "waldur_core.server",
            "worker",
            "--concurrency=4",
            "--without-gossip",
        ]
    r = docker("run", "-d", "--name", name, *env, *cmd)
    if r.returncode:
        raise RuntimeError(f"docker run failed: {r.stderr}")


def sample(name: str) -> dict:
    r = docker("exec", name, "python3", "-c", SAMPLER)
    if r.returncode:
        raise RuntimeError(f"sampler failed: {r.stderr or r.stdout}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def wait_booted(name: str, mode: str, want: int, timeout: int = 90) -> dict:
    """Poll until at least `want` matched processes exist and PSS stabilises."""
    deadline = time.time() + timeout
    last = {}
    stable = 0
    while time.time() < deadline:
        try:
            s = sample(name)
        except RuntimeError:
            time.sleep(2)
            continue
        ready = s["nproc"] >= want
        if ready and last and abs(s["sum_pss_mb"] - last.get("sum_pss_mb", 0)) < 5:
            stable += 1
            if stable >= 2:
                return s
        else:
            stable = 0
        last = s
        time.sleep(3)
    return last


def upsert(label: str, mode: str, s: dict, preload: str | None) -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    fields = [
        "label",
        "mode",
        "preload",
        "nproc",
        "cgroup_current_mb",
        "sum_pss_mb",
        "sum_rss_mb",
    ]
    rows = []
    if RUNTIME_CSV.exists():
        with RUNTIME_CSV.open() as fh:
            rows = [
                r
                for r in csv.DictReader(fh)
                if not (r["label"] == label and r["mode"] == mode)
            ]
    rows.append(
        {
            "label": label,
            "mode": mode,
            "preload": preload if preload is not None else "baked",
            "nproc": s.get("nproc", 0),
            "cgroup_current_mb": s.get("cgroup_current_mb", 0),
            "sum_pss_mb": s.get("sum_pss_mb", 0),
            "sum_rss_mb": s.get("sum_rss_mb", 0),
        }
    )
    with RUNTIME_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--mode", choices=["api", "celery"], default="api")
    ap.add_argument(
        "--preload",
        choices=["true", "false"],
        default=None,
        help="override GUNICORN_PRELOAD (needs env-driven conf); default uses image's baked config",
    )
    ap.add_argument(
        "--ld-preload",
        default=None,
        help="override LD_PRELOAD; pass '' to disable jemalloc, 'libjemalloc.so.2' to force it",
    )
    ap.add_argument("--want-procs", type=int, default=4)
    args = ap.parse_args()

    name = f"waldur-mem-{args.label}-{args.mode}"
    docker("rm", "-f", name)
    try:
        start_container(name, args.image, args.mode, args.preload, args.ld_preload)
        print(f"[{args.label}/{args.mode}] booting workers ...")
        s = wait_booted(name, args.mode, args.want_procs)
        if not s:
            print("ERROR: container never reported processes; logs:", file=sys.stderr)
            print(docker("logs", "--tail", "30", name).stdout, file=sys.stderr)
            return 1
        upsert(args.label, args.mode, s, args.preload)
        print(
            f"  nproc={s['nproc']}  cgroup_current={s['cgroup_current_mb']} MB  "
            f"sum_pss={s['sum_pss_mb']} MB  sum_rss={s['sum_rss_mb']} MB"
        )
        print(f"  -> {RUNTIME_CSV}")
    finally:
        docker("rm", "-f", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
