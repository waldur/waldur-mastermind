#!/usr/bin/env python
"""Render visual memory comparisons from the harness CSVs.

Reads ``memory_profiles/startup_summary.csv`` and
``memory_profiles/startup_components.csv`` (written by
``measure_startup_memory.py``) and produces:

* ``heatmap-components.png`` -- components (rows) x variants (columns), colour =
  MB. The "who is consuming memory" view; extra columns appear as you add
  variants (baseline, jemalloc, preload, ...).
* ``delta-heatmap.png`` -- per-component change vs baseline (diverging colour),
  only when more than one variant exists. Shows what each change moved.
* ``peak-rss.png`` -- peak startup memory per variant (bar chart).
* ``MEMORY_RESULTS.md`` -- summary table (peak + delta vs baseline).

Run with matplotlib available, e.g.:
    uv run --with matplotlib python scripts/plot_memory_comparison.py
"""

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

OUTDIR = Path(__file__).resolve().parent / "memory_profiles"
SUMMARY_CSV = OUTDIR / "startup_summary.csv"
COMPONENTS_CSV = OUTDIR / "startup_components.csv"
RUNTIME_CSV = OUTDIR / "runtime_summary.csv"
PREFERRED = ["baseline", "jemalloc", "preload", "celery-cap"]
RUNTIME_ORDER = ["baseline", "opt-noopts", "jemalloc", "preload", "preload-jemalloc"]
TOP_N = 25


def ordered_labels(labels: set[str]) -> list[str]:
    head = [x for x in PREFERRED if x in labels]
    tail = sorted(labels - set(head))
    return head + tail


def load():
    summary = {}
    with SUMMARY_CSV.open() as fh:
        for r in csv.DictReader(fh):
            summary[r["label"]] = float(r["peak_rss_mb"])
    comps: dict[tuple[str, str], float] = {}
    names: set[str] = set()
    with COMPONENTS_CSV.open() as fh:
        for r in csv.DictReader(fh):
            comps[(r["label"], r["component"])] = float(r["mb"])
            names.add(r["component"])
    return summary, comps, names


def build_matrix(comps, names, labels):
    """Return (component_rows, matrix[rows x labels]) limited to TOP_N rows."""
    totals = {n: max(comps.get((lab, n), 0.0) for lab in labels) for n in names}
    rows = sorted(totals, key=lambda n: -totals[n])[:TOP_N]
    mat = np.array([[comps.get((lab, n), 0.0) for lab in labels] for n in rows])
    return rows, mat


def heatmap(rows, mat, labels, path, *, diverging=False, title=""):
    h = max(4.0, 0.34 * len(rows) + 1.5)
    w = max(5.0, 1.7 * len(labels) + 3.0)
    fig, ax = plt.subplots(figsize=(w, h))
    if diverging:
        lim = max(1.0, np.abs(mat).max())
        im = ax.imshow(mat, aspect="auto", cmap="RdBu", vmin=-lim, vmax=lim)
        fmt = "{:+.1f}"
    else:
        im = ax.imshow(mat, aspect="auto", cmap="YlOrRd")
        fmt = "{:.1f}"
    ax.set_xticks(range(len(labels)), labels, rotation=30, ha="right")
    ax.set_yticks(range(len(rows)), rows, fontsize=8)
    thr = mat.max() if not diverging else np.abs(mat).max()
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            if abs(v) < 1e-9:
                continue
            shade = abs(v) / thr if thr else 0
            ax.text(
                j,
                i,
                fmt.format(v),
                ha="center",
                va="center",
                fontsize=6.5,
                color="white" if shade > 0.6 else "black",
            )
    ax.set_title(title or "Startup memory by component (MB)")
    fig.colorbar(im, ax=ax, label="MB" + (" vs baseline" if diverging else ""))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  wrote {path}")


def peak_bar(summary, labels, path):
    vals = [summary[lab] for lab in labels]
    fig, ax = plt.subplots(figsize=(max(4, 1.2 * len(labels) + 2), 4))
    bars = ax.bar(labels, vals, color="#4C78A8")
    base = summary.get("baseline")
    for b, v in zip(bars, vals):
        lbl = f"{v:.0f}"
        if base and labels and b is not bars[0]:
            lbl += f"\n({v - base:+.0f})"
        ax.text(
            b.get_x() + b.get_width() / 2, v, lbl, ha="center", va="bottom", fontsize=9
        )
    ax.set_ylabel("Peak startup RSS (MB)")
    ax.set_title("Startup peak memory per variant")
    ax.margins(y=0.15)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  wrote {path}")


def write_markdown(summary, labels, path):
    base = summary.get("baseline")
    lines = [
        "# Memory results",
        "",
        "## Startup peak RSS",
        "",
        "| Variant | Peak RSS (MB) | Δ vs baseline |",
        "| --- | ---: | ---: |",
    ]
    for lab in labels:
        v = summary[lab]
        delta = (
            "—"
            if (base is None or lab == "baseline")
            else f"{v - base:+.1f} ({(v - base) / base * 100:+.1f}%)"
        )
        lines.append(f"| {lab} | {v:.1f} | {delta} |")
    lines += [
        "",
        f"_Top {TOP_N} components: see heatmap-components.png. Flamegraphs: flamegraph-<variant>.html_",
        "",
    ]
    path.write_text("\n".join(lines))
    print(f"  wrote {path}")


def runtime_chart(path, mode, title) -> str | None:
    if not RUNTIME_CSV.exists():
        return None
    rows = {}
    with RUNTIME_CSV.open() as fh:
        for r in csv.DictReader(fh):
            if r["mode"] == mode:
                rows[r["label"]] = r
    if not rows:
        return None
    labels = [lab for lab in RUNTIME_ORDER if lab in rows] + sorted(
        set(rows) - set(RUNTIME_ORDER)
    )
    cgroup = [float(rows[lab]["cgroup_current_mb"]) for lab in labels]
    pss = [float(rows[lab]["sum_pss_mb"]) for lab in labels]

    x = np.arange(len(labels))
    w = 0.38
    fig, ax = plt.subplots(figsize=(max(6, 1.5 * len(labels) + 2), 4.5))
    b1 = ax.bar(
        x - w / 2, cgroup, w, label="cgroup memory.current (k8s/OOM)", color="#E45756"
    )
    b2 = ax.bar(x + w / 2, pss, w, label="sum PSS (CoW-aware)", color="#4C78A8")
    base = cgroup[labels.index("baseline")] if "baseline" in labels else None
    for bars, vals in ((b1, cgroup), (b2, pss)):
        for b, v in zip(bars, vals):
            ax.text(
                b.get_x() + b.get_width() / 2,
                v,
                f"{v:.0f}",
                ha="center",
                va="bottom",
                fontsize=8,
            )
    ax.set_xticks(x, labels, rotation=20, ha="right")
    ax.set_ylabel("Pod memory (MB)")
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.margins(y=0.15)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  wrote {path}")

    lines = [
        "",
        f"## Runtime memory — {title}",
        "",
        "| Variant | cgroup current (MB) | Δ vs baseline | sum PSS (MB) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for lab, c, p in zip(labels, cgroup, pss):
        delta = (
            "—"
            if (base is None or lab == "baseline")
            else f"{c - base:+.0f} ({(c - base) / base * 100:+.0f}%)"
        )
        lines.append(f"| {lab} | {c:.0f} | {delta} | {p:.0f} |")
    return "\n".join(lines)


def main() -> int:
    summary, comps, names = load()
    labels = ordered_labels(set(summary))
    rows, mat = build_matrix(comps, names, labels)

    heatmap(rows, mat, labels, OUTDIR / "heatmap-components.png")
    peak_bar(summary, labels, OUTDIR / "peak-rss.png")
    write_markdown(summary, labels, OUTDIR / "MEMORY_RESULTS.md")

    for mode, fname, title in (
        ("api", "runtime-memory.png", "API pod (gunicorn, 4 workers)"),
        (
            "celery",
            "runtime-memory-celery.png",
            "Celery worker pod (prefork, concurrency 4)",
        ),
    ):
        runtime_md = runtime_chart(OUTDIR / fname, mode, title)
        if runtime_md:
            with (OUTDIR / "MEMORY_RESULTS.md").open("a") as fh:
                fh.write(runtime_md + "\n")

    if "baseline" in labels and len(labels) > 1:
        bcol = labels.index("baseline")
        delta = mat - mat[:, [bcol]]
        others = [lab for lab in labels if lab != "baseline"]
        idx = [labels.index(lab) for lab in others]
        heatmap(
            rows,
            delta[:, idx],
            others,
            OUTDIR / "delta-heatmap.png",
            diverging=True,
            title="Component memory change vs baseline (MB)",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
