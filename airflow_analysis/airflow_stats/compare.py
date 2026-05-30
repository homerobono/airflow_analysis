"""Cross-configuration comparisons.

This module answers the question "which fan setup gives better airflow?" by
holding workload roughly constant: it compares each config's median sensor
values within matched load-decile bins.

It also computes a crude "fan efficiency" score per fan: the slope of the
hotspot temperature with respect to fan RPM, estimated within high-load samples
where the fan actually matters. A more negative slope means more cooling per
RPM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .loader import ConfigData
from .stats import LOAD_BIN_LABELS, PerConfigStats


@dataclass
class ComparisonResult:
    baseline: str                                       # name of reference config
    configs: list[str]                                  # all config names in order
    matched_cpu: dict[str, pd.DataFrame] = field(default_factory=dict)
    matched_gpu: dict[str, pd.DataFrame] = field(default_factory=dict)
    delta_cpu: dict[str, pd.DataFrame] = field(default_factory=dict)
    delta_gpu: dict[str, pd.DataFrame] = field(default_factory=dict)
    fan_efficiency: pd.DataFrame = field(default_factory=pd.DataFrame)
    insights: list[str] = field(default_factory=list)


# --- canonical hotspot labels (in display order) -----------------------------
_HOTSPOT_DISPLAY_ORDER = [
    "cpu_pkg", "cpu_ccd1",
    "gpu_core", "gpu_hotspot", "gpu_mem_junction",
    "vrm", "chipset", "system",
]


def _hotspot_temp_matrix(
    stats_by_name: dict[str, PerConfigStats],
    which: str,  # "cpu" or "gpu"
) -> dict[str, pd.DataFrame]:
    """For each config, return a DataFrame [bin x hotspot_label] of median temps.

    Columns use the friendly hotspot key (cpu_pkg, gpu_hotspot, ...). Rows are
    the load-decile bin labels so configs can be subtracted bin-for-bin.
    """
    out: dict[str, pd.DataFrame] = {}
    attr = "load_binned_cpu" if which == "cpu" else "load_binned_gpu"
    for name, st in stats_by_name.items():
        binned: pd.DataFrame = getattr(st, attr)
        if binned.empty:
            out[name] = pd.DataFrame()
            continue
        # Reindex to a canonical bin order to keep subtraction safe.
        binned = binned.reindex(LOAD_BIN_LABELS)
        cols: dict[str, pd.Series] = {}
        for hot_key in _HOTSPOT_DISPLAY_ORDER:
            sensor = st.primary_temps.get(hot_key)
            if sensor is None or sensor not in binned.columns:
                continue
            cols[hot_key] = binned[sensor]
        if "__n__" in binned.columns:
            cols["__n__"] = binned["__n__"]
        out[name] = pd.DataFrame(cols)
    return out


def _delta_vs_baseline(
    matched: dict[str, pd.DataFrame], baseline: str
) -> dict[str, pd.DataFrame]:
    base = matched.get(baseline)
    if base is None or base.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}
    for name, df in matched.items():
        if name == baseline or df.empty:
            continue
        common_cols = [c for c in df.columns if c in base.columns and c != "__n__"]
        if not common_cols:
            continue
        delta = df[common_cols].subtract(base[common_cols])
        out[name] = delta
    return out


def _fan_efficiency(
    data_by_name: dict[str, ConfigData],
    stats_by_name: dict[str, PerConfigStats],
    high_load_threshold: float = 50.0,
) -> pd.DataFrame:
    """Slope of hotspot temp vs each fan RPM under high load.

    A more negative slope = more cooling per additional RPM. Reported per
    (config, fan, hotspot) triple. Only computed when both columns vary.
    """
    rows: list[dict[str, float | str]] = []
    for name, data in data_by_name.items():
        st = stats_by_name[name]
        df = data.df
        cpu_load = st.primary_loads.get("cpu_total")
        gpu_load = st.primary_loads.get("gpu_core")
        # Restrict to "warm" samples to avoid the idle plateau dominating.
        mask = pd.Series(True, index=df.index)
        if cpu_load and cpu_load in df.columns:
            mask &= pd.to_numeric(df[cpu_load], errors="coerce") >= high_load_threshold
        elif gpu_load and gpu_load in df.columns:
            mask &= pd.to_numeric(df[gpu_load], errors="coerce") >= high_load_threshold
        sub = df.loc[mask]
        if len(sub) < 30:
            sub = df  # fall back to full data if filter is too strict

        for hot_key in ("cpu_pkg", "gpu_hotspot", "vrm"):
            temp_col = st.primary_temps.get(hot_key)
            if temp_col is None or temp_col not in sub.columns:
                continue
            for fan in st.schema.fans_rpm():
                if fan not in sub.columns:
                    continue
                x = pd.to_numeric(sub[fan], errors="coerce")
                y = pd.to_numeric(sub[temp_col], errors="coerce")
                joined = pd.concat([x, y], axis=1).dropna()
                if len(joined) < 30:
                    continue
                xv = joined.iloc[:, 0].to_numpy()
                yv = joined.iloc[:, 1].to_numpy()
                if xv.std() < 1.0 or yv.std() < 0.1:
                    continue
                # OLS slope (degC per RPM).
                slope, intercept = np.polyfit(xv, yv, 1)
                # Pearson r for context.
                r = float(np.corrcoef(xv, yv)[0, 1])
                rows.append(
                    {
                        "config": name,
                        "hotspot": hot_key,
                        "fan": fan,
                        "fan_label": data.slot_label(fan),
                        "slope_C_per_RPM": float(slope),
                        "slope_C_per_1000_RPM": float(slope) * 1000.0,
                        "pearson_r": r,
                        "n": int(len(joined)),
                    }
                )
    return pd.DataFrame(rows)


def _generate_insights(
    delta_cpu: dict[str, pd.DataFrame],
    delta_gpu: dict[str, pd.DataFrame],
    fan_eff: pd.DataFrame,
    baseline: str,
    stats_by_name: dict[str, PerConfigStats],
    data_by_name: dict[str, ConfigData],
) -> list[str]:
    """Produce a ranked, plain-English insight list."""
    out: list[str] = []

    # Largest cross-config improvements/regressions vs baseline.
    def _flatten(deltas: dict[str, pd.DataFrame], which: str) -> list[tuple[str, str, str, float]]:
        rows: list[tuple[str, str, str, float]] = []
        for cfg, df in deltas.items():
            if df.empty:
                continue
            stacked = df.drop(columns=[c for c in df.columns if c == "__n__"], errors="ignore").stack()
            for (bin_label, hot_key), val in stacked.items():
                if pd.isna(val):
                    continue
                rows.append((which, cfg, f"{hot_key}@{bin_label}", float(val)))
        return rows

    flat = _flatten(delta_cpu, "CPU-load") + _flatten(delta_gpu, "GPU-load")
    flat.sort(key=lambda r: r[3])
    improvements = [r for r in flat if r[3] < -0.5][:8]
    regressions = [r for r in flat if r[3] > 0.5][-8:][::-1]

    if improvements:
        out.append(
            "Top airflow gains vs Config "
            + baseline
            + ": "
            + "; ".join(
                f"Config {cfg} cools {hot} by {abs(d):.1f} C ({band} bin)"
                for band, cfg, hot, d in improvements
            )
            + "."
        )
    if regressions:
        out.append(
            "Worst regressions vs Config "
            + baseline
            + ": "
            + "; ".join(
                f"Config {cfg} runs {hot} +{d:.1f} C hotter ({band} bin)"
                for band, cfg, hot, d in regressions
            )
            + "."
        )

    # Best & worst fan efficiency per config (treating CPU package).
    if not fan_eff.empty:
        for cfg, sub in fan_eff.groupby("config"):
            cpu_sub = sub[sub["hotspot"] == "cpu_pkg"]
            if cpu_sub.empty:
                continue
            best = cpu_sub.loc[cpu_sub["slope_C_per_1000_RPM"].idxmin()]
            if best["slope_C_per_1000_RPM"] < -0.1 and best["pearson_r"] < -0.1:
                out.append(
                    f"Config {cfg}: most effective fan for CPU is "
                    f"'{best['fan_label']}' "
                    f"({best['slope_C_per_1000_RPM']:.2f} C drop per 1000 RPM, "
                    f"r={best['pearson_r']:.2f})."
                )

    # Redundant fans: highly correlated with another fan but only weakly with any temp.
    for name, st in stats_by_name.items():
        fans = st.schema.fans_rpm()
        temps = list(st.primary_temps.values())
        if st.corr_spearman.empty or len(fans) < 2 or not temps:
            continue
        corr = st.corr_spearman
        fan_pairs: list[tuple[str, str, float]] = []
        for i, a in enumerate(fans):
            if a not in corr.columns:
                continue
            for b in fans[i + 1 :]:
                if b not in corr.columns:
                    continue
                r = corr.loc[a, b]
                if pd.notna(r) and abs(r) > 0.95:
                    # Are either of them strongly correlated with a hotspot temp?
                    max_temp_corr = 0.0
                    for t in temps:
                        if t in corr.columns:
                            ra = abs(corr.loc[a, t]) if pd.notna(corr.loc[a, t]) else 0.0
                            rb = abs(corr.loc[b, t]) if pd.notna(corr.loc[b, t]) else 0.0
                            max_temp_corr = max(max_temp_corr, ra, rb)
                    if max_temp_corr < 0.3:
                        fan_pairs.append((a, b, float(r)))
        if fan_pairs:
            data = data_by_name[name]
            samples = "; ".join(
                f"'{data.label(a)}' ~ '{data.label(b)}' (r={r:.2f})"
                for a, b, r in fan_pairs[:3]
            )
            out.append(
                f"Config {name}: possibly redundant fan pairs (move together but "
                f"don't track any hotspot): {samples}."
            )

    # Mismatched curves: fan running hard with no temp drop.
    if not fan_eff.empty:
        weak = fan_eff[
            (fan_eff["pearson_r"].abs() < 0.1)
            & (fan_eff["hotspot"] == "cpu_pkg")
        ]
        for _, row in weak.iterrows():
            out.append(
                f"Config {row['config']}: fan '{row['fan_label']}' shows no thermal "
                f"effect on CPU package (r={row['pearson_r']:.2f}); consider "
                f"lowering its curve."
            )

    return out


def compare_configs(
    data_by_name: dict[str, ConfigData],
    stats_by_name: dict[str, PerConfigStats],
) -> ComparisonResult:
    """Run full cross-config comparison.

    The first config (alphabetically) is used as the baseline for delta tables.
    """
    names = list(stats_by_name.keys())
    if not names:
        return ComparisonResult(baseline="", configs=[])
    baseline = names[0]

    matched_cpu = _hotspot_temp_matrix(stats_by_name, "cpu")
    matched_gpu = _hotspot_temp_matrix(stats_by_name, "gpu")
    delta_cpu = _delta_vs_baseline(matched_cpu, baseline)
    delta_gpu = _delta_vs_baseline(matched_gpu, baseline)
    fan_eff = _fan_efficiency(data_by_name, stats_by_name)
    insights = _generate_insights(
        delta_cpu, delta_gpu, fan_eff, baseline, stats_by_name, data_by_name
    )

    return ComparisonResult(
        baseline=baseline,
        configs=names,
        matched_cpu=matched_cpu,
        matched_gpu=matched_gpu,
        delta_cpu=delta_cpu,
        delta_gpu=delta_gpu,
        fan_efficiency=fan_eff,
        insights=insights,
    )
