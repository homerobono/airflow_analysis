"""Assemble per-config and cross-config analyses into a single HTML report."""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path

import jinja2
import pandas as pd

from . import __version__
from .compare import ComparisonResult
from .cooling import (
    CoolingKPIs,
    compute_cooling_kpis,
    kpis_to_detail_rows,
    kpis_to_summary_rows,
    rank_configs,
)
from .fan_impact import (
    FanImpactMatrix,
    best_fan_per_target,
    compute_fan_impact,
    matrix_to_rows,
)
from .loader import ConfigData
from .plotting import (
    correlation_heatmap,
    fan_impact_heatmap,
    load_binned_heatmap,
    lowess_scatter,
    time_series,
    violin_temps,
)
from .stats import PerConfigStats

_logger = logging.getLogger(__name__)


def _df_to_html(df: pd.DataFrame, float_fmt: str = "{:.2f}") -> str:
    if df is None or df.empty:
        return "<p class='meta'>No data.</p>"
    return df.to_html(
        classes="data",
        border=0,
        float_format=float_fmt.format,
        na_rep="",
    )


def _summary_html(stats: PerConfigStats, data: ConfigData) -> str:
    df = stats.summary
    if df.empty:
        return "<p class='meta'>No sensors available.</p>"
    # Limit to the most interesting rows for the report (else it's huge).
    schema = stats.schema
    key_paths = (
        list(stats.primary_temps.values())
        + schema.mb_temps
        + schema.ram_temps
        + schema.storage_temps
        + schema.fans_rpm()
        + list(stats.primary_loads.values())
    )
    key_paths = list(dict.fromkeys([p for p in key_paths if p in df.index]))
    if not key_paths:
        return "<p class='meta'>No headline sensors detected.</p>"
    sub = df.loc[key_paths].copy()
    sub = sub.reset_index(drop=False).rename(columns={"index": "sensor"})
    sub["sensor"] = sub["sensor"].apply(lambda s: data.label(s) or s)
    sub = sub.drop(columns=["label"], errors="ignore")
    return sub.to_html(
        classes="data",
        border=0,
        index=False,
        float_format=lambda v: f"{v:.2f}",
        na_rep="",
    )


def _rename_with_labels(df: pd.DataFrame, labels: dict[str, str]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.rename(columns={c: labels.get(c, c) for c in df.columns})


def _hotspot_label_map() -> dict[str, str]:
    return {
        "cpu_pkg": "CPU (Tctl/Tdie)",
        "cpu_ccd1": "CPU CCD1",
        "gpu_core": "GPU Core",
        "gpu_hotspot": "GPU Hot Spot",
        "gpu_mem_junction": "GPU Mem Junction",
        "vrm": "VRM MOS",
        "chipset": "Chipset",
        "system": "System",
        "dimm1": "DIMM #1",
        "dimm3": "DIMM #3",
    }


def _per_config_payload(data: ConfigData, stats: PerConfigStats) -> dict:
    labels = data.labels
    hotspot_map = _hotspot_label_map()

    # Violin distributions of hotspot temps.
    series_by_label: dict[str, pd.Series] = {}
    for key, sensor in stats.primary_temps.items():
        if sensor in data.df.columns:
            series_by_label[hotspot_map.get(key, key)] = data.df[sensor]
    violin = violin_temps(series_by_label, f"Hotspot temperatures - Config {data.name}")

    # Load-binned heatmaps with friendly column names.
    cpu_bins = stats.load_binned_cpu.rename(columns={
        v: hotspot_map.get(k, k) for k, v in stats.primary_temps.items()
    })
    cpu_bins = cpu_bins.rename(columns={c: labels.get(c, c) for c in cpu_bins.columns})
    gpu_bins = stats.load_binned_gpu.rename(columns={
        v: hotspot_map.get(k, k) for k, v in stats.primary_temps.items()
    })
    gpu_bins = gpu_bins.rename(columns={c: labels.get(c, c) for c in gpu_bins.columns})

    plot_cpu_load = load_binned_heatmap(
        cpu_bins, f"Median temps & fan RPM vs CPU load - Config {data.name}", "value"
    )
    plot_gpu_load = load_binned_heatmap(
        gpu_bins, f"Median temps & fan RPM vs GPU load - Config {data.name}", "value"
    )

    # Fan curves: CPU fan vs CPU pkg, plus 1-2 system fans vs hotspots.
    fan_curves: list[str] = []
    cpu_pkg = stats.primary_temps.get("cpu_pkg")
    gpu_hot = stats.primary_temps.get("gpu_hotspot") or stats.primary_temps.get("gpu_core")

    def _find_by_label(candidates: list[str], hint: str) -> str | None:
        for c in candidates:
            if hint.lower() in (labels.get(c, "") or "").lower():
                return c
        return None

    cpu_fan = _find_by_label(stats.schema.fans_rpm(), "cpu fan")
    if cpu_fan and cpu_pkg:
        img = lowess_scatter(
            data.df[cpu_pkg],
            data.df[cpu_fan],
            x_label=f"{labels.get(cpu_pkg, cpu_pkg)} (C)",
            y_label=f"{labels.get(cpu_fan, cpu_fan)} (RPM)",
            title=f"CPU fan curve - Config {data.name}",
        )
        if img:
            fan_curves.append(img)

    # First couple of system fans against GPU hotspot - prefer fans that
    # actually spin (skip headers whose RPM column is flat zero).
    sys_fans = [c for c in stats.schema.mb_fans_rpm if "system fan" in (labels.get(c, "") or "").lower()]
    sys_fans = [c for c in sys_fans if pd.to_numeric(data.df[c], errors="coerce").std() > 0]
    for fan in sys_fans[:2]:
        if gpu_hot:
            img = lowess_scatter(
                data.df[gpu_hot],
                data.df[fan],
                x_label=f"{labels.get(gpu_hot, gpu_hot)} (C)",
                y_label=f"{labels.get(fan, fan)} (RPM)",
                title=f"{labels.get(fan, fan)} vs GPU Hot Spot - Config {data.name}",
            )
            if img:
                fan_curves.append(img)

    # Correlation heatmap (Spearman) over the most informative columns.
    corr_plot = correlation_heatmap(
        stats.corr_spearman,
        labels=labels,
        title=f"Spearman correlation - Config {data.name}",
        min_abs=0.3,
        max_size=25,
    )

    # Fan Impact Matrix: which fan position cools which temperature, after
    # controlling for load, power, ambient, and the other fans.
    fan_impact = compute_fan_impact(data, stats)
    fan_impact_plot = fan_impact_heatmap(
        fan_impact.impact,
        fan_impact.tolerance,
        title=f"Fan Impact Matrix - Config {data.name}",
    )
    fan_impact_rows = matrix_to_rows(fan_impact)
    for r in fan_impact_rows:
        r["fmt"] = {
            "c_per_1000_rpm": _fmt(r["c_per_1000_rpm"], 2, " C/1000 RPM"),
            "tolerance": _fmt(r["tolerance"], 2),
        }
    fan_impact_best = []
    for target, info in best_fan_per_target(fan_impact).items():
        fan_impact_best.append(
            {
                "target": target,
                "fan": info["fan"],
                "c_per_1000_rpm": info["c_per_1000_rpm"],
                "tolerance": info["tolerance"],
                "n": info["n"],
                "fmt": {
                    "c_per_1000_rpm": _fmt(info["c_per_1000_rpm"], 2, " C/1000 RPM"),
                    "tolerance": _fmt(info["tolerance"], 2),
                },
            }
        )
    r2_rows: list[dict[str, object]] = []
    if not fan_impact.r2_per_target.empty:
        for target, r2 in fan_impact.r2_per_target.dropna().items():
            r2_rows.append({"target": target, "r2": float(r2), "r2_fmt": _fmt(r2, 2)})

    # Lag table.
    lag_html = ""
    if not stats.lag_table.empty:
        lt = stats.lag_table.copy()
        lt["load"] = lt["load"].replace({"cpu_total": "CPU total", "gpu_core": "GPU core"})
        lt["temp"] = lt["temp"].map(lambda k: hotspot_map.get(k, k))
        lt = lt[["load", "temp", "best_lag_samples", "best_lag_seconds", "best_corr"]]
        lt.columns = ["Load", "Temp", "Lag (samples)", "Lag (s)", "Pearson r"]
        lag_html = lt.to_html(
            classes="data",
            border=0,
            index=False,
            float_format=lambda v: f"{v:.2f}",
            na_rep="",
        )

    # Hotspot timeline.
    timeline_cols = [v for v in stats.primary_temps.values() if v in data.df.columns][:6]
    timeline_plot = time_series(
        data.df,
        timeline_cols,
        labels={c: labels.get(c, c) for c in timeline_cols},
        title=f"Hotspot timeline - Config {data.name}",
    )

    return {
        "name": data.name,
        "n_csvs": len(data.csv_files),
        "n_rows": data.n_rows,
        "n_sensors": data.df.shape[1],
        "sample_period_s": stats.sample_period_s,
        "has_fan_map": data.setup.has_fan_map,
        "fan_map": data.setup.fan_map,
        "notes": data.setup.notes,
        "summary_html": _summary_html(stats, data),
        "plot_violin": violin,
        "plot_cpu_load_bins": plot_cpu_load,
        "plot_gpu_load_bins": plot_gpu_load,
        "plot_fan_curves": fan_curves,
        "plot_corr": corr_plot,
        "plot_fan_impact": fan_impact_plot,
        "fan_impact_rows": fan_impact_rows,
        "fan_impact_best": fan_impact_best,
        "fan_impact_r2": r2_rows,
        "fan_impact_notes": fan_impact.notes,
        "lag_html": lag_html,
        "plot_timeline": timeline_plot,
    }


def _comparison_payload(comparison: ComparisonResult) -> dict:
    hotspot_map = _hotspot_label_map()

    matched_plots: list[str] = []
    for name, df in comparison.matched_cpu.items():
        img = load_binned_heatmap(
            df, f"Median temps (CPU-load bins) - Config {name}", "C"
        )
        if img:
            matched_plots.append(img)
    for name, df in comparison.matched_gpu.items():
        img = load_binned_heatmap(
            df, f"Median temps (GPU-load bins) - Config {name}", "C"
        )
        if img:
            matched_plots.append(img)

    delta_plots: list[str] = []
    for name, df in comparison.delta_cpu.items():
        img = load_binned_heatmap(
            df, f"Delta vs Config {comparison.baseline} (CPU-load) - {name}",
            "Delta C", diverging=True,
        )
        if img:
            delta_plots.append(img)
    for name, df in comparison.delta_gpu.items():
        img = load_binned_heatmap(
            df, f"Delta vs Config {comparison.baseline} (GPU-load) - {name}",
            "Delta C", diverging=True,
        )
        if img:
            delta_plots.append(img)

    fan_eff = comparison.fan_efficiency.copy()
    fan_eff_html = ""
    if not fan_eff.empty:
        fan_eff["hotspot"] = fan_eff["hotspot"].map(lambda k: hotspot_map.get(k, k))
        fan_eff = fan_eff[
            ["config", "hotspot", "fan_label", "slope_C_per_1000_RPM",
             "pearson_r", "n"]
        ].rename(columns={
            "config": "Config",
            "hotspot": "Hotspot",
            "fan_label": "Fan",
            "slope_C_per_1000_RPM": "C / 1000 RPM",
            "pearson_r": "Pearson r",
            "n": "n",
        })
        fan_eff_html = fan_eff.sort_values(
            ["Config", "Hotspot", "C / 1000 RPM"]
        ).to_html(
            classes="data",
            border=0,
            index=False,
            float_format=lambda v: f"{v:.3f}",
            na_rep="",
        )

    return {
        "baseline": comparison.baseline,
        "configs": comparison.configs,
        "matched_plots": matched_plots,
        "delta_plots": delta_plots,
        "fan_eff_html": fan_eff_html,
    }


def _fmt(value: float, digits: int = 1, suffix: str = "") -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if f != f:  # NaN
        return "n/a"
    return f"{f:.{digits}f}{suffix}"


def _cooling_payload(kpis: list[CoolingKPIs]) -> dict:
    """Render-ready bundle of cooling KPIs."""
    if not kpis:
        return {"summary_rows": [], "detail_rows": [], "ranking": [], "winner": None}

    summary = kpis_to_summary_rows(kpis)
    detail = kpis_to_detail_rows(kpis)
    ranking = rank_configs(kpis)

    # Decorate with pre-formatted strings so the template stays trivial.
    for row in summary:
        row["fmt"] = {
            "cooling_score": _fmt(row["cooling_score"], 1),
            "ambient_median": _fmt(row["ambient_median"], 1, " C"),
            "cpu_high_load_median": _fmt(row["cpu_high_load_median"], 1, " C"),
            "cpu_rise": _fmt(row["cpu_rise"], 1, " C"),
            "cpu_c_per_w": _fmt(row["cpu_c_per_w"], 3, " C/W"),
            "cpu_headroom": _fmt(row["cpu_headroom"], 1, " C"),
            "gpu_high_load_median": _fmt(row["gpu_high_load_median"], 1, " C"),
            "gpu_rise": _fmt(row["gpu_rise"], 1, " C"),
            "gpu_c_per_w": _fmt(row["gpu_c_per_w"], 3, " C/W"),
            "gpu_headroom": _fmt(row["gpu_headroom"], 1, " C"),
            "median_fan_rpm_total": _fmt(row["median_fan_rpm_total"], 0, " RPM"),
            "high_load_fan_rpm_total": _fmt(row["high_load_fan_rpm_total"], 0, " RPM"),
            "high_load_fraction_pct": _fmt(
                (row["high_load_fraction"] or 0) * 100.0, 0, "%"
            ),
            "workload_score": _fmt(row["workload_score"], 0),
            "workload_cpu_intensity": _fmt(row["workload_cpu_intensity"], 0),
            "workload_gpu_intensity": _fmt(row["workload_gpu_intensity"], 0),
            "workload_cpu_clock_ghz": _fmt(row["workload_cpu_clock_ghz"], 2, " GHz"),
            "workload_cpu_vcore_v": _fmt(row["workload_cpu_vcore_v"], 3, " V"),
            "workload_cpu_pkg_w": _fmt(row["workload_cpu_pkg_w"], 1, " W"),
            "workload_gpu_clock_mhz": _fmt(row["workload_gpu_clock_mhz"], 0, " MHz"),
            "workload_gpu_voltage_v": _fmt(row["workload_gpu_voltage_v"], 3, " V"),
            "workload_gpu_pkg_w": _fmt(row["workload_gpu_pkg_w"], 1, " W"),
        }

    for row in detail:
        row["fmt"] = {
            "median": _fmt(row["median"], 1),
            "high_load_median": _fmt(row["high_load_median"], 1),
            "p95": _fmt(row["p95"], 1),
            "rise_over_ambient": _fmt(row["rise_over_ambient"], 1),
            "c_per_watt": _fmt(row["c_per_watt"], 3),
            "median_power_w": _fmt(row["median_power_w"], 1),
            "headroom": _fmt(row["headroom"], 1),
        }

    winner = ranking[0][0] if ranking else None
    multi = len(kpis) > 1

    return {
        "summary_rows": summary,
        "detail_rows": detail,
        "ranking": [
            {"name": name, "score": score, "score_fmt": _fmt(score, 1)}
            for name, score in ranking
        ],
        "winner": winner,
        "multi": multi,
    }


def render_report(
    root: Path,
    configs: list[tuple[ConfigData, PerConfigStats]],
    comparison: ComparisonResult | None,
    output: Path,
) -> Path:
    """Render report.html with everything inlined."""
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("airflow_stats", "templates"),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.j2")

    per_config = [_per_config_payload(d, s) for d, s in configs]
    comp_payload = _comparison_payload(comparison) if comparison and comparison.configs else None

    kpis = [compute_cooling_kpis(d, s) for d, s in configs]
    cooling_payload = _cooling_payload(kpis)

    html = template.render(
        generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=str(root),
        configs=per_config,
        insights=(comparison.insights if comparison else []),
        comparison=comp_payload,
        cooling=cooling_payload,
        version=__version__,
    )

    output.write_text(html, encoding="utf-8")
    return output
