#!/usr/bin/env python
"""Measure Waldur startup/import memory and attribute it per component.

Profiles ``waldur check`` (which imports every installed app and runs system
checks ~ the minimum work to start the process) under memray, then:

* writes an interactive flamegraph HTML (visual "what costs what"),
* attributes peak (high-watermark) memory to each plugin / SDK / core component
  by walking each allocation's stack and charging it to the outermost Waldur app
  that pulled it in (falling back to the innermost third-party package),
* appends a per-component breakdown and a peak-memory summary row to CSVs keyed
  by ``--label`` so several variants accumulate for side-by-side comparison.

Run once per variant (baseline, jemalloc, preload, ...), then render the
heatmap / charts with ``plot_memory_comparison.py``.

Usage:
    uv run python scripts/measure_startup_memory.py --label baseline
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_SETTINGS = "waldur_core.server.base_settings"
OUTDIR = Path(__file__).resolve().parent / "memory_profiles"
SUMMARY_CSV = OUTDIR / "startup_summary.csv"
COMPONENTS_CSV = OUTDIR / "startup_components.csv"


def component_for(filename: str) -> str:
    """Map a source filename to a coarse component name."""
    if not filename:
        return "stdlib/other"
    f = filename.replace("\\", "/")
    for marker in ("/site-packages/", "/dist-packages/"):
        if marker in f:
            top = f.split(marker, 1)[1].split("/", 1)[0]
            if top.endswith(".py"):
                top = top[:-3]
            return f"pkg:{top}"
    if "/src/" in f:
        parts = f.split("/src/", 1)[1].split("/")
        top = parts[0]
        # Give the mastermind/core mega-packages finer per-app granularity.
        if top in ("waldur_mastermind", "waldur_core") and len(parts) > 1:
            return f"{top}.{parts[1]}"
        return top
    return "stdlib/other"


# Generic loader/scaffolding frames that sit above every app import; charging
# memory to them hides the real owner, so we skip past them to the allocation site.
SCAFFOLD = ("waldur_core.server", "waldur_core.__init__", "waldur_core.core")


def attribute(frames) -> str:
    """Charge an allocation to the innermost meaningful component.

    Walks from the allocation site (innermost frame) outward and returns the
    first real component -- the third-party package or Waldur app that actually
    holds the memory -- skipping stdlib/import machinery and the generic Django
    bootstrap scaffolding. This is the "who is consuming memory" view.
    """
    comps = [component_for(fname) for (_func, fname, _line) in frames]
    for c in comps:  # innermost first
        if c == "stdlib/other" or c in SCAFFOLD:
            continue
        return c
    for c in comps:  # any non-stdlib as fallback (incl. scaffolding)
        if c != "stdlib/other":
            return c
    return "stdlib/other"


def run_memray(label: str, settings: str, bin_path: Path) -> None:
    waldur_bin = shutil.which("waldur")
    if not waldur_bin:
        raise RuntimeError("`waldur` not found on PATH")
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": settings}
    print(f"[{label}] profiling `waldur check` under memray ...")
    # `python -m memray` rather than the bare console script so it resolves in
    # any environment (CI included) where only the module is importable.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "memray",
            "run",
            "--force",
            "-o",
            str(bin_path),
            waldur_bin,
            "check",
        ],
        env=env,
        check=True,
    )


def make_flamegraph(bin_path: Path, html_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "memray",
            "flamegraph",
            "--force",
            "-o",
            str(html_path),
            str(bin_path),
        ],
        check=True,
    )
    print(f"  flamegraph -> {html_path}")


def analyze(bin_path: Path):
    """Return (peak_mb, total_allocations, {component: bytes})."""
    from memray import FileReader

    reader = FileReader(str(bin_path))
    peak_bytes = reader.metadata.peak_memory
    total_allocs = reader.metadata.total_allocations

    by_component: dict[str, int] = {}
    for record in reader.get_high_watermark_allocation_records(merge_threads=True):
        frames = record.stack_trace()
        comp = attribute(frames) if frames else "stdlib/other"
        by_component[comp] = by_component.get(comp, 0) + record.size
    return peak_bytes / 1024 / 1024, total_allocs, by_component


def upsert_summary(label: str, peak_mb: float, total_allocs: int) -> None:
    rows = []
    if SUMMARY_CSV.exists():
        with SUMMARY_CSV.open() as fh:
            rows = [r for r in csv.DictReader(fh) if r["label"] != label]
    rows.append(
        {
            "label": label,
            "peak_rss_mb": f"{peak_mb:.1f}",
            "total_allocations": total_allocs,
        }
    )
    with SUMMARY_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["label", "peak_rss_mb", "total_allocations"])
        w.writeheader()
        w.writerows(rows)


def upsert_components(label: str, by_component: dict[str, int]) -> None:
    rows = []
    if COMPONENTS_CSV.exists():
        with COMPONENTS_CSV.open() as fh:
            rows = [r for r in csv.DictReader(fh) if r["label"] != label]
    for comp, size in sorted(by_component.items(), key=lambda kv: -kv[1]):
        rows.append(
            {
                "label": label,
                "component": comp,
                "bytes": size,
                "mb": f"{size / 1024 / 1024:.2f}",
            }
        )
    with COMPONENTS_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["label", "component", "bytes", "mb"])
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="variant name, e.g. baseline")
    parser.add_argument("--settings", default=DEFAULT_SETTINGS)
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="reuse the existing .bin for --label instead of re-profiling",
    )
    parser.add_argument(
        "--budget-mb",
        type=float,
        default=float(os.environ.get("MEMORY_BUDGET_MB", 0)),
        help="fail (exit 2) if peak RSS exceeds this; 0 disables the gate",
    )
    args = parser.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    bin_path = OUTDIR / f"{args.label}.bin"
    html_path = OUTDIR / f"flamegraph-{args.label}.html"

    if not args.skip_run:
        run_memray(args.label, args.settings, bin_path)
    if not bin_path.exists():
        print(f"ERROR: no capture at {bin_path}", file=sys.stderr)
        return 1

    make_flamegraph(bin_path, html_path)
    peak_mb, total_allocs, by_component = analyze(bin_path)
    upsert_summary(args.label, peak_mb, total_allocs)
    upsert_components(args.label, by_component)

    print(f"\n[{args.label}] peak high-watermark memory: {peak_mb:.1f} MB")
    print("  top components:")
    for comp, size in sorted(by_component.items(), key=lambda kv: -kv[1])[:15]:
        print(f"    {size / 1024 / 1024:8.2f} MB  {comp}")
    print(f"\n  summary    -> {SUMMARY_CSV}")
    print(f"  components -> {COMPONENTS_CSV}")

    if args.budget_mb and peak_mb > args.budget_mb:
        print(
            f"\nFAIL: peak {peak_mb:.1f} MB exceeds budget {args.budget_mb:.1f} MB",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
