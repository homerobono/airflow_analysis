"""Print the cooling-score variants table to stdout.

Thin CLI wrapper around ``airflow_stats.cooling.compute_score_variants``.
Same numbers that appear in the "Score sensitivity (V0..V5 variants)"
section of report.html, but in your terminal so you can iterate without
rebuilding the full report.

V5 is the production cooling score; V0..V4 are listed for transparency.

Run:
    cd /Users/hbonomini/Libremonitor
    source .venv/bin/activate
    python airflow_analysis/scripts/score_variants.py
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from airflow_stats.cooling import compute_score_variants
from airflow_stats.loader import load_all
from airflow_stats.stats import analyze_config


_VARIANT_IDS = ["V0", "V1", "V2", "V3", "V4", "V5"]


def _fmt(x: float | None, width: int = 6, prec: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return f"{'--':>{width}}"
    return f"{x:>{width}.{prec}f}"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    root = Path(__file__).resolve().parents[2]
    datas = load_all(root)
    if not datas:
        print(f"No Config */ folders found under {root}")
        return 1

    rows: list[dict] = []
    for data in datas:
        stats = analyze_config(data)
        rows.append({"name": data.name, "variants": compute_score_variants(data, stats)})

    print()
    print("=" * 100)
    print(" Cooling score under different 'high resource utilization' definitions")
    print("=" * 100)
    print()
    header = (
        f"{'config':<10} "
        + " ".join(f"{vid:>7}" for vid in _VARIANT_IDS)
        + "    " + f"{'V5 cpu':>8} {'V5 gpu':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        v5 = r["variants"]["V5"]
        cpu_cov = (
            f"{v5['cpu_coverage'] * 100:>7.1f}%"
            if v5.get("cpu_active") else f"{'idle':>8}"
        )
        gpu_cov = (
            f"{v5['gpu_coverage'] * 100:>7.1f}%"
            if v5.get("gpu_active") else f"{'idle':>8}"
        )
        scores = " ".join(
            _fmt(r["variants"][vid].get("score"), 7) for vid in _VARIANT_IDS
        )
        print(f"{r['name']:<10} {scores}    {cpu_cov} {gpu_cov}")

    print()
    print("Sub-scores per variant:")
    print()
    sub_header = (
        f"{'config':<10} {'variant':<14} {'temp':>6} {'rpm':>6} "
        f"{'rise_C':>7} {'cpu_C':>6} {'gpu_C':>6} {'vrm_C':>6} {'fanRPM':>7} {'n':>8}"
    )
    print(sub_header)
    print("-" * len(sub_header))
    for r in rows:
        for vid in _VARIANT_IDS:
            v = r["variants"][vid]
            print(
                f"{r['name']:<10} {v['label']:<14} "
                f"{_fmt(v.get('temp_score'), 6)} "
                f"{_fmt(v.get('rpm_score'), 6)} "
                f"{_fmt(v.get('weighted_rise'), 7, 2)} "
                f"{_fmt(v.get('cpu_rise'), 6, 1)} "
                f"{_fmt(v.get('gpu_rise'), 6, 1)} "
                f"{_fmt(v.get('vrm_rise'), 6, 1)} "
                f"{_fmt(v.get('high_total_rpm'), 7, 0)} "
                f"{v.get('n_samples', 0):>8d}"
            )

    print()
    print("Notes:")
    for vid in _VARIANT_IDS:
        any_row = rows[0]["variants"][vid]
        print(f"  {vid}: {any_row.get('gate', '')}")
    print()
    print("V4 is NOT a cooling score (it measures input load, not cooling output).")
    print("It's listed to confirm whether two captures are under similar pressure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
