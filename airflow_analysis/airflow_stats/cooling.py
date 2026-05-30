"""Cooling-performance KPIs.

The goal is to score each config on *how cool it runs proportional to the
resources being used*. Raw averages are misleading when configs are measured
under different workloads, so every KPI here normalizes against load or power.

Key per-config metrics
----------------------
* ``temp_rise_over_ambient``: hotspot median minus motherboard "System" temp.
  Approximates how much heat the cooling solution is failing to remove.
* ``c_per_watt``: (hotspot - ambient) divided by the matching component's
  median power draw under load. Lower = more efficient cooling per watt.
* ``high_load_median``: hotspot median when the matching load is >= 70%.
  Workload-controlled "stress" temperature.
* ``thermal_headroom``: distance from the p95 temperature to a typical
  throttling threshold (CPU 95 C, GPU hot spot 90 C, VRM 100 C).
* ``acoustic_cost``: median of the sum of all fan RPMs under high load,
  a proxy for noise. Cooling that needs less RPM is "better airflow".
* ``cooling_score``: V5 composite. Per-component matched-quantile masks
  (each config scored on its own top-30% busiest CPU samples for CPU rise,
  top-30% busiest GPU samples for GPU rise, union for VRM and fan RPM),
  goal-aligned weights (cpu=0.55, gpu=0.30, vrm=0.15 in the temp budget;
  temp=0.80, rpm=0.20 in the composite), idle-component drop (a component
  whose 70th-percentile load is < 5% is removed from the temp term and the
  remaining weights renormalized). Higher = cooler / quieter for the
  things that matter, fairly compared across captures with different
  workload distributions. ``compute_score_variants`` exposes V0..V4 too,
  for transparency in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .loader import ConfigData
from .stats import PerConfigStats

# Throttling thresholds used to compute headroom (deg C).
_THROTTLE_LIMITS: dict[str, float] = {
    "cpu_pkg": 95.0,
    "cpu_ccd1": 95.0,
    "gpu_core": 88.0,
    "gpu_hotspot": 90.0,
    "gpu_mem_junction": 95.0,
    "vrm": 100.0,
}

# High-load gates (percent). Restrict "stress" KPIs to actually-loaded samples.
# These are intentionally asymmetric: powerful CPUs can heat up significantly
# even at light loads, so the CPU gate is set very low to catch that regime.
# GPUs only really warm up once they're actively rendering, so the GPU gate
# is higher to filter out near-idle samples.
_HIGH_LOAD_PCT_CPU = 10.0
_HIGH_LOAD_PCT_GPU = 30.0
# Backwards-compatible default used when the caller doesn't specify a target.
_HIGH_LOAD_PCT = _HIGH_LOAD_PCT_CPU

# ----- V5 production score parameters ----------------------------------------
# V5 scores each config against its OWN busiest moments per component, rather
# than against an absolute load threshold, so captures with different workload
# distributions still get an apples-to-apples comparison.
_V5_QUANTILE = 0.30          # top 30% of loaded samples per component, per config
_V5_MIN_LOAD_FLOOR = 5.0     # if the (1-q)-percentile load is below this %,
                             # treat the component as idle for this capture
_V5_CPU_W = 0.55             # CPU weight in the temperature budget
_V5_GPU_W = 0.30             # GPU weight
_V5_VRM_W = 0.15             # VRM weight
_V5_TEMP_W = 0.80            # temperature term weight in composite
_V5_RPM_W = 0.20             # fan-RPM term weight in composite

# Workload-intensity normalization anchors used by V2 (intensity gate).
_V2_INTENSITY_THRESHOLD = 0.7

# Mapping hotspot key -> (load_key, power_keyword) used to compute C/W.
_HOTSPOT_POWER_HINTS: dict[str, tuple[str, list[str]]] = {
    "cpu_pkg": ("cpu_total", ["cpu package", "package power", "cpu pkg", "cpu cores"]),
    "gpu_hotspot": ("gpu_core", ["gpu power", "gpu total", "board power"]),
    "gpu_core": ("gpu_core", ["gpu power", "gpu total", "board power"]),
}


@dataclass
class HotspotKPI:
    """Cooling KPIs for a single hotspot inside one config."""

    hotspot_key: str             # "cpu_pkg", "gpu_hotspot", ...
    hotspot_label: str           # "CPU (Tctl/Tdie)"
    median_temp: float           # overall median (C)
    high_load_median: float      # median when load >= 70% (C)
    p95_temp: float              # 95th percentile (C)
    temp_rise_over_ambient: float  # hotspot_median - system_median (C)
    c_per_watt: float            # (hotspot - ambient) / median power, C/W
    thermal_headroom: float      # throttle_limit - p95 (C, larger = safer)
    median_power_w: float        # for context


@dataclass
class WorkloadIntensity:
    """How hard the system was actually being pushed under load.

    All sub-scores are normalized to 0..1 (1 = pushing the silicon hard).
    ``overall`` is their mean; ``score`` is overall * 100 for display.
    """

    cpu_clock_ghz: float                 # median CPU "Cores (Average)" under load
    cpu_vcore_v: float                   # median Vcore under load
    cpu_package_w: float                 # median CPU package power under load
    gpu_clock_mhz: float                 # median GPU core clock under load
    gpu_voltage_v: float                 # median GPU core voltage under load
    gpu_power_w: float                   # median GPU package power under load
    cpu_intensity: float                 # 0..1
    gpu_intensity: float                 # 0..1
    overall: float                       # 0..1 average of available sub-scores
    score: float                         # overall * 100


@dataclass
class CoolingKPIs:
    """All cooling-performance KPIs for one config."""

    name: str
    ambient_median: float                # motherboard "System" temp median (C)
    high_load_mask_fraction: float       # fraction of samples in high-load gate
    median_total_fan_rpm: float          # sum of all fan RPMs, median
    high_load_total_fan_rpm: float       # same, restricted to high-load
    cooling_score: float                 # 0..100, higher = cooler per resource
    cooling_score_is_v5: bool = True     # False when we fell back to V0 (capture
                                         # too idle for matched-quantile gating)
    workload: WorkloadIntensity | None = None
    hotspots: list[HotspotKPI] = field(default_factory=list)


# ---------- helpers -----------------------------------------------------------


def _median_numeric(series: pd.Series) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return float("nan")
    return float(s.median())


def _find_power_column(
    data: ConfigData, stats: PerConfigStats, keywords: list[str]
) -> str | None:
    """Pick the first power channel whose friendly label matches any keyword."""
    power_cols = stats.schema.power()
    for col in power_cols:
        lbl = (data.labels.get(col, "") or "").lower()
        for kw in keywords:
            if kw in lbl:
                return col
    return power_cols[0] if power_cols else None


def _gate_for(load_key: str) -> float:
    """Pick the high-load gate threshold for a given load channel."""
    if load_key == "cpu_total":
        return _HIGH_LOAD_PCT_CPU
    if load_key == "gpu_core":
        return _HIGH_LOAD_PCT_GPU
    return _HIGH_LOAD_PCT


def _compute_high_load_mask(
    data: ConfigData, stats: PerConfigStats, load_key: str
) -> pd.Series:
    """Boolean mask of samples where the named load is >= the high-load gate."""
    load_col = stats.primary_loads.get(load_key)
    if load_col is None or load_col not in data.df.columns:
        return pd.Series(False, index=data.df.index)
    series = pd.to_numeric(data.df[load_col], errors="coerce")
    return series >= _gate_for(load_key)


def _hotspot_kpi(
    data: ConfigData,
    stats: PerConfigStats,
    hotspot_key: str,
    hotspot_label: str,
    ambient_median: float,
) -> HotspotKPI | None:
    temp_col = stats.primary_temps.get(hotspot_key)
    if temp_col is None or temp_col not in data.df.columns:
        return None
    temps = pd.to_numeric(data.df[temp_col], errors="coerce").dropna()
    if temps.empty:
        return None

    load_key, power_kw = _HOTSPOT_POWER_HINTS.get(
        hotspot_key, ("cpu_total", ["package power"])
    )
    high_mask = _compute_high_load_mask(data, stats, load_key)
    high_temps = pd.to_numeric(data.df.loc[high_mask, temp_col], errors="coerce").dropna()

    median_temp = float(temps.median())
    high_load_median = float(high_temps.median()) if not high_temps.empty else float("nan")
    p95 = float(temps.quantile(0.95))

    rise = (
        median_temp - ambient_median
        if not np.isnan(ambient_median)
        else float("nan")
    )

    power_col = _find_power_column(data, stats, power_kw)
    median_power = float("nan")
    c_per_w = float("nan")
    if power_col is not None and power_col in data.df.columns:
        power_series = pd.to_numeric(data.df.loc[high_mask, power_col], errors="coerce")
        power_series = power_series[power_series > 1.0]  # ignore idle samples
        if power_series.empty:
            power_series = pd.to_numeric(data.df[power_col], errors="coerce")
            power_series = power_series[power_series > 1.0]
        if not power_series.empty:
            median_power = float(power_series.median())
            if not np.isnan(rise) and median_power > 0:
                c_per_w = rise / median_power

    limit = _THROTTLE_LIMITS.get(hotspot_key)
    headroom = (limit - p95) if limit is not None else float("nan")

    return HotspotKPI(
        hotspot_key=hotspot_key,
        hotspot_label=hotspot_label,
        median_temp=median_temp,
        high_load_median=high_load_median,
        p95_temp=p95,
        temp_rise_over_ambient=rise,
        c_per_watt=c_per_w,
        thermal_headroom=headroom,
        median_power_w=median_power,
    )


def _total_fan_rpm(
    data: ConfigData, stats: PerConfigStats, mask: pd.Series | None
) -> float:
    fans = [c for c in stats.schema.fans_rpm() if c in data.df.columns]
    if not fans:
        return float("nan")
    sub = data.df[fans].apply(pd.to_numeric, errors="coerce")
    if mask is not None:
        sub = sub.loc[mask]
    if sub.empty:
        return float("nan")
    total = sub.sum(axis=1, skipna=True)
    return float(total.median())


def _label_for(key: str) -> str:
    return {
        "cpu_pkg": "CPU (Tctl/Tdie)",
        "cpu_ccd1": "CPU CCD1",
        "gpu_core": "GPU Core",
        "gpu_hotspot": "GPU Hot Spot",
        "gpu_mem_junction": "GPU Mem Junction",
        "vrm": "VRM MOS",
        "chipset": "Chipset",
        "system": "System",
    }.get(key, key)


# ---------- workload intensity ----------------------------------------------

# Anchors for normalizing each workload signal to 0..1. Tuned for modern
# desktop AMD/Intel CPUs and mid-to-high-end GPUs; configs that exceed these
# anchors just clip to 1.0 (which only matters if you ever want to compare a
# heavily-OC'd run vs a stock one - the score still treats both as "max load").
_CPU_CLOCK_MAX_MHZ = 5000.0      # boost target
_CPU_VCORE_LO_V, _CPU_VCORE_HI_V = 0.90, 1.40
_CPU_PKG_MAX_W = 150.0
_GPU_CLOCK_MAX_MHZ = 3000.0
_GPU_VOLTAGE_LO_V, _GPU_VOLTAGE_HI_V = 0.70, 1.10
_GPU_PKG_MAX_W = 350.0


def _pick_first_label_match(
    data: ConfigData, candidates: list[str], hints: list[str]
) -> str | None:
    for col in candidates:
        if col not in data.df.columns:
            continue
        lbl = (data.labels.get(col, "") or "").lower()
        for h in hints:
            if h in lbl:
                return col
    return None


def _median_under_mask(
    data: ConfigData, col: str | None, mask: pd.Series
) -> float:
    if col is None or col not in data.df.columns:
        return float("nan")
    s = pd.to_numeric(data.df[col], errors="coerce")
    if mask is not None and mask.any():
        s = s.loc[mask]
    s = s.dropna()
    if s.empty:
        return float("nan")
    return float(s.median())


def _norm(value: float, lo: float, hi: float) -> float:
    if np.isnan(value):
        return float("nan")
    if hi <= lo:
        return float("nan")
    x = (value - lo) / (hi - lo)
    return float(max(0.0, min(1.0, x)))


def _compute_workload_intensity(
    data: ConfigData, stats: PerConfigStats, high_mask: pd.Series
) -> WorkloadIntensity:
    """Quantify how hard the silicon was being pushed during high-load samples.

    Uses clocks, voltages, and power if those sensors are present. Sub-scores
    that can't be computed are simply omitted from the average.
    """
    schema = stats.schema

    cpu_clock_col = _pick_first_label_match(
        data, schema.cpu_clocks,
        ["cores (average)", "core (average)", "cpu cores", "core #1"],
    )
    cpu_vcore_col = _pick_first_label_match(
        data, schema.cpu_voltages, ["vcore", "core voltage", "cpu core"],
    )
    if cpu_vcore_col is None:
        # AMD CPUs often expose per-core VIDs only; fall back to the
        # motherboard's Vcore reading from the SuperIO chip.
        cpu_vcore_col = _pick_first_label_match(
            data, schema.mb_voltages, ["vcore", "cpu vcore", "cpu core"],
        )
    if cpu_vcore_col is None and schema.cpu_voltages:
        # Last resort: average of all per-core VID readings is a reasonable
        # proxy for what the cores are actually being fed.
        vid_cols = [
            c for c in schema.cpu_voltages
            if "vid" in (data.labels.get(c, "") or "").lower()
        ]
        if vid_cols:
            cpu_vcore_col = vid_cols[0]  # the first VID is usually fine for "is this an aggressive run?"
    cpu_pkg_power_col = _pick_first_label_match(
        data, schema.cpu_power, ["package", "cpu package", "cpu pkg"],
    )

    gpu_clock_col = _pick_first_label_match(
        data, schema.gpu_clocks, ["gpu core", "gpu clock", "core"],
    )
    gpu_voltage_col = _pick_first_label_match(
        data, schema.gpu_voltages, ["gpu core", "core"],
    )
    gpu_power_col = _pick_first_label_match(
        data, schema.gpu_power, ["package", "gpu power", "board", "total"],
    )

    cpu_clock_mhz = _median_under_mask(data, cpu_clock_col, high_mask)
    cpu_vcore = _median_under_mask(data, cpu_vcore_col, high_mask)
    cpu_pkg_w = _median_under_mask(data, cpu_pkg_power_col, high_mask)
    gpu_clock_mhz = _median_under_mask(data, gpu_clock_col, high_mask)
    gpu_voltage = _median_under_mask(data, gpu_voltage_col, high_mask)
    gpu_pkg_w = _median_under_mask(data, gpu_power_col, high_mask)

    cpu_subs = [
        _norm(cpu_clock_mhz, 0.0, _CPU_CLOCK_MAX_MHZ),
        _norm(cpu_vcore, _CPU_VCORE_LO_V, _CPU_VCORE_HI_V),
        _norm(cpu_pkg_w, 0.0, _CPU_PKG_MAX_W),
    ]
    gpu_subs = [
        _norm(gpu_clock_mhz, 0.0, _GPU_CLOCK_MAX_MHZ),
        _norm(gpu_voltage, _GPU_VOLTAGE_LO_V, _GPU_VOLTAGE_HI_V),
        _norm(gpu_pkg_w, 0.0, _GPU_PKG_MAX_W),
    ]
    cpu_avail = [v for v in cpu_subs if not np.isnan(v)]
    gpu_avail = [v for v in gpu_subs if not np.isnan(v)]
    cpu_intensity = float(np.mean(cpu_avail)) if cpu_avail else float("nan")
    gpu_intensity = float(np.mean(gpu_avail)) if gpu_avail else float("nan")
    parts = [v for v in (cpu_intensity, gpu_intensity) if not np.isnan(v)]
    overall = float(np.mean(parts)) if parts else float("nan")

    return WorkloadIntensity(
        cpu_clock_ghz=cpu_clock_mhz / 1000.0 if not np.isnan(cpu_clock_mhz) else float("nan"),
        cpu_vcore_v=cpu_vcore,
        cpu_package_w=cpu_pkg_w,
        gpu_clock_mhz=gpu_clock_mhz,
        gpu_voltage_v=gpu_voltage,
        gpu_power_w=gpu_pkg_w,
        cpu_intensity=cpu_intensity,
        gpu_intensity=gpu_intensity,
        overall=overall,
        score=overall * 100.0 if not np.isnan(overall) else float("nan"),
    )


# ---------- score variants (V0 legacy + V1..V5) ------------------------------
#
# V5 is the production cooling_score. V0..V4 are exposed via
# ``compute_score_variants`` for transparency in the report - so the user can
# see how the score moves under different "high resource utilization"
# definitions. The single-source-of-truth for variant logic lives here so the
# report and the standalone debug script (scripts/score_variants.py) agree.


def _per_sample_intensity_mask(
    data: ConfigData, stats: PerConfigStats, threshold: float = _V2_INTENSITY_THRESHOLD
) -> pd.Series:
    """V2 mask: samples where max(CPU, GPU) per-sample workload intensity >= threshold.

    Mirrors ``_compute_workload_intensity`` column-pick logic but works
    row-wise instead of taking medians under an existing mask.
    """
    schema = stats.schema

    cpu_clock_col = _pick_first_label_match(
        data, schema.cpu_clocks,
        ["cores (average)", "core (average)", "cpu cores", "core #1"],
    )
    cpu_vcore_col = _pick_first_label_match(
        data, schema.cpu_voltages, ["vcore", "core voltage", "cpu core"],
    )
    if cpu_vcore_col is None:
        cpu_vcore_col = _pick_first_label_match(
            data, schema.mb_voltages, ["vcore", "cpu vcore", "cpu core"],
        )
    if cpu_vcore_col is None and schema.cpu_voltages:
        vid_cols = [
            c for c in schema.cpu_voltages
            if "vid" in (data.labels.get(c, "") or "").lower()
        ]
        if vid_cols:
            cpu_vcore_col = vid_cols[0]
    cpu_pkg_power_col = _pick_first_label_match(
        data, schema.cpu_power, ["package", "cpu package", "cpu pkg"],
    )
    gpu_clock_col = _pick_first_label_match(
        data, schema.gpu_clocks, ["gpu core", "gpu clock", "core"],
    )
    gpu_voltage_col = _pick_first_label_match(
        data, schema.gpu_voltages, ["gpu core", "core"],
    )
    gpu_power_col = _pick_first_label_match(
        data, schema.gpu_power, ["package", "gpu power", "board", "total"],
    )

    df = data.df
    idx = df.index

    def _norm_col(col: str | None, lo: float, hi: float) -> pd.Series:
        if col is None or col not in df.columns:
            return pd.Series(np.nan, index=idx)
        if hi <= lo:
            return pd.Series(np.nan, index=idx)
        x = (pd.to_numeric(df[col], errors="coerce") - lo) / (hi - lo)
        return x.clip(lower=0.0, upper=1.0)

    cpu_parts = pd.concat(
        [
            _norm_col(cpu_clock_col, 0.0, _CPU_CLOCK_MAX_MHZ),
            _norm_col(cpu_vcore_col, _CPU_VCORE_LO_V, _CPU_VCORE_HI_V),
            _norm_col(cpu_pkg_power_col, 0.0, _CPU_PKG_MAX_W),
        ],
        axis=1,
    )
    gpu_parts = pd.concat(
        [
            _norm_col(gpu_clock_col, 0.0, _GPU_CLOCK_MAX_MHZ),
            _norm_col(gpu_voltage_col, _GPU_VOLTAGE_LO_V, _GPU_VOLTAGE_HI_V),
            _norm_col(gpu_power_col, 0.0, _GPU_PKG_MAX_W),
        ],
        axis=1,
    )

    cpu_intensity = cpu_parts.mean(axis=1, skipna=True)
    gpu_intensity = gpu_parts.mean(axis=1, skipna=True)
    combined = pd.concat([cpu_intensity, gpu_intensity], axis=1).max(axis=1, skipna=True)
    return combined.fillna(0.0) >= threshold


def _per_component_quantile_masks(
    data: ConfigData, stats: PerConfigStats, q: float = _V5_QUANTILE
) -> dict[str, pd.Series | None]:
    """V5 masks: per-component matched-quantile boolean masks.

    Returns ``{"cpu": cpu_mask | None, "gpu": gpu_mask | None,
    "vrmfan": union | None}``. A component is None when its
    (1-q)-percentile load is below ``_V5_MIN_LOAD_FLOOR`` percent (idle), so
    the score correctly drops that component instead of admitting every
    sample.
    """
    df = data.df
    idx = df.index

    def _top_q(col: str | None) -> pd.Series | None:
        if col is None or col not in df.columns:
            return None
        s = pd.to_numeric(df[col], errors="coerce")
        valid = s.dropna()
        if valid.empty:
            return None
        threshold = float(valid.quantile(1.0 - q))
        if threshold < _V5_MIN_LOAD_FLOOR:
            return None
        return (s >= threshold).fillna(False)

    cpu_mask = _top_q(stats.primary_loads.get("cpu_total"))
    gpu_mask = _top_q(stats.primary_loads.get("gpu_core"))
    if cpu_mask is not None and gpu_mask is not None:
        vrmfan_mask = cpu_mask | gpu_mask
    elif cpu_mask is not None:
        vrmfan_mask = cpu_mask
    elif gpu_mask is not None:
        vrmfan_mask = gpu_mask
    else:
        vrmfan_mask = None
    return {"cpu": cpu_mask, "gpu": gpu_mask, "vrmfan": vrmfan_mask}


def _median_df_under_mask(
    df: pd.DataFrame, col: str | None, mask: pd.Series | None
) -> float:
    """DataFrame-keyed counterpart to ``_median_under_mask`` (which takes ConfigData)."""
    if col is None or col not in df.columns:
        return float("nan")
    s = pd.to_numeric(df[col], errors="coerce")
    if mask is not None:
        s = s.loc[mask]
    s = s.dropna()
    return float(s.median()) if not s.empty else float("nan")


def _total_fan_rpm_under_mask(
    data: ConfigData, stats: PerConfigStats, mask: pd.Series | None
) -> float:
    fans = [c for c in stats.schema.fans_rpm() if c in data.df.columns]
    if not fans:
        return float("nan")
    sub = data.df[fans].apply(pd.to_numeric, errors="coerce")
    if mask is not None:
        sub = sub.loc[mask]
    if sub.empty:
        return float("nan")
    return float(sub.sum(axis=1, skipna=True).median())


def _score_v5_full(
    data: ConfigData, stats: PerConfigStats, ambient_median: float
) -> dict[str, object]:
    """Full V5 result dict: score plus diagnostics for the variants table.

    Per-component matched-quantile masks; goal-aligned weights
    (cpu=0.55 / gpu=0.30 / vrm=0.15 in temp budget; temp=0.80 / rpm=0.20 in
    composite); no workload-intensity forgiveness; idle components dropped.
    """
    masks = _per_component_quantile_masks(data, stats, q=_V5_QUANTILE)

    def _rise(hot_key: str, mask: pd.Series | None) -> float:
        if mask is None:
            return float("nan")
        col = stats.primary_temps.get(hot_key)
        if col is None or col not in data.df.columns:
            return float("nan")
        med = _median_df_under_mask(data.df, col, mask)
        if np.isnan(med) or np.isnan(ambient_median):
            return float("nan")
        return med - ambient_median

    cpu_rise = _rise("cpu_pkg", masks["cpu"])
    gpu_rise = _rise("gpu_hotspot", masks["gpu"])
    if np.isnan(gpu_rise):
        gpu_rise = _rise("gpu_core", masks["gpu"])
    vrm_rise = _rise("vrm", masks["vrmfan"])

    component_weights = [
        (cpu_rise, _V5_CPU_W),
        (gpu_rise, _V5_GPU_W),
        (vrm_rise, _V5_VRM_W),
    ]
    available = [(r, w) for r, w in component_weights if not np.isnan(r)]
    if available:
        total_w = sum(w for _, w in available)
        weighted_rise = sum(r * w for r, w in available) / total_w
        temp_score = max(0.0, min(100.0, 100.0 - (weighted_rise / 40.0) * 100.0))
    else:
        weighted_rise = float("nan")
        temp_score = float("nan")

    if masks["vrmfan"] is not None:
        high_total_rpm = _total_fan_rpm_under_mask(data, stats, masks["vrmfan"])
    else:
        high_total_rpm = float("nan")
    if not np.isnan(high_total_rpm):
        rpm_score = max(0.0, min(100.0, 100.0 - (high_total_rpm / 8000.0) * 100.0))
    else:
        rpm_score = float("nan")

    if not np.isnan(temp_score) and not np.isnan(rpm_score):
        score = _V5_TEMP_W * temp_score + _V5_RPM_W * rpm_score
    elif not np.isnan(temp_score):
        score = temp_score
    else:
        score = float("nan")

    total = int(len(data.df.index))
    cpu_n = int(masks["cpu"].sum()) if masks["cpu"] is not None else 0
    gpu_n = int(masks["gpu"].sum()) if masks["gpu"] is not None else 0
    vrmfan_n = int(masks["vrmfan"].sum()) if masks["vrmfan"] is not None else 0

    return {
        "score": score,
        "temp_score": temp_score,
        "rpm_score": rpm_score,
        "weighted_rise": weighted_rise,
        "cpu_rise": cpu_rise,
        "gpu_rise": gpu_rise,
        "vrm_rise": vrm_rise,
        "high_total_rpm": high_total_rpm,
        "n_total": total,
        "cpu_n": cpu_n,
        "gpu_n": gpu_n,
        "n_samples": vrmfan_n,
        "cpu_coverage": (cpu_n / total) if total else 0.0,
        "gpu_coverage": (gpu_n / total) if total else 0.0,
        "coverage": (vrmfan_n / total) if total else 0.0,
        "cpu_active": masks["cpu"] is not None,
        "gpu_active": masks["gpu"] is not None,
    }


def _score_with_mask(
    data: ConfigData,
    stats: PerConfigStats,
    high_mask: pd.Series,
    workload_overall: float,
    *,
    forgive_intensity: bool,
    cpu_w: float = 4.0 / 7.0,
    gpu_w: float = 3.0 / 7.0,
    vrm_w: float = 1.0 / 7.0,
    temp_w: float = 0.70,
    rpm_w: float = 0.30,
    ambient_median: float | None = None,
) -> dict[str, object]:
    """Composite cooling score on an arbitrary high-load mask.

    Used by V0/V1/V2/V3 variants in ``compute_score_variants``. The default
    weights and ``forgive_intensity=True`` reproduce the legacy V0 formula
    (production score before V5).
    """
    if ambient_median is None:
        system_col = stats.primary_temps.get("system")
        ambient_median = (
            _median_numeric(data.df[system_col])
            if system_col and system_col in data.df.columns
            else float("nan")
        )

    def _rise(hot_key: str) -> float:
        col = stats.primary_temps.get(hot_key)
        if col is None or col not in data.df.columns:
            return float("nan")
        med = _median_df_under_mask(data.df, col, high_mask)
        if np.isnan(med) or np.isnan(ambient_median):
            return float("nan")
        return med - ambient_median

    cpu_rise = _rise("cpu_pkg")
    gpu_rise = _rise("gpu_hotspot")
    if np.isnan(gpu_rise):
        gpu_rise = _rise("gpu_core")
    vrm_rise = _rise("vrm")

    component_weights = [
        (cpu_rise, cpu_w),
        (gpu_rise, gpu_w),
        (vrm_rise, vrm_w),
    ]
    available = [(r, w) for r, w in component_weights if not np.isnan(r)]
    if available:
        total_w = sum(w for _, w in available)
        weighted_rise = sum(r * w for r, w in available) / total_w
        if forgive_intensity and not np.isnan(workload_overall):
            adjusted_rise = max(0.0, weighted_rise - 10.0 * workload_overall)
        else:
            adjusted_rise = max(0.0, weighted_rise)
        temp_score = max(0.0, min(100.0, 100.0 - (adjusted_rise / 40.0) * 100.0))
    else:
        weighted_rise = float("nan")
        temp_score = float("nan")

    high_total_rpm = _total_fan_rpm_under_mask(data, stats, high_mask)
    if not np.isnan(high_total_rpm):
        rpm_score = max(0.0, min(100.0, 100.0 - (high_total_rpm / 8000.0) * 100.0))
    else:
        rpm_score = float("nan")

    if not np.isnan(temp_score) and not np.isnan(rpm_score):
        score = temp_w * temp_score + rpm_w * rpm_score
    elif not np.isnan(temp_score):
        score = temp_score
    else:
        score = float("nan")

    n = int(high_mask.sum()) if high_mask is not None else 0
    total = int(len(high_mask)) if high_mask is not None else 0
    return {
        "score": score,
        "temp_score": temp_score,
        "rpm_score": rpm_score,
        "weighted_rise": weighted_rise,
        "cpu_rise": cpu_rise,
        "gpu_rise": gpu_rise,
        "vrm_rise": vrm_rise,
        "high_total_rpm": high_total_rpm,
        "n_samples": n,
        "n_total": total,
        "coverage": (n / total) if total else 0.0,
    }


def compute_score_variants(
    data: ConfigData, stats: PerConfigStats
) -> dict[str, dict[str, object]]:
    """Return V0..V5 scores for one config, for the report's sensitivity panel.

    V5 is the production score (same value as ``CoolingKPIs.cooling_score``);
    the others are listed for transparency, so the user can see how the
    ranking moves under different "high resource utilization" definitions.
    """
    system_col = stats.primary_temps.get("system")
    ambient_median = (
        _median_numeric(data.df[system_col])
        if system_col and system_col in data.df.columns
        else float("nan")
    )

    cpu_high = _compute_high_load_mask(data, stats, "cpu_total")
    gpu_high = _compute_high_load_mask(data, stats, "gpu_core")
    legacy_mask = cpu_high | gpu_high
    workload = _compute_workload_intensity(data, stats, legacy_mask)
    workload_overall = (
        workload.overall
        if workload and not np.isnan(workload.overall)
        else float("nan")
    )

    cpu_load_col = stats.primary_loads.get("cpu_total")
    gpu_load_col = stats.primary_loads.get("gpu_core")

    def _ge(col: str | None, threshold: float) -> pd.Series:
        if col is None or col not in data.df.columns:
            return pd.Series(False, index=data.df.index)
        s = pd.to_numeric(data.df[col], errors="coerce")
        return (s >= threshold).fillna(False)

    v0 = _score_with_mask(
        data, stats, legacy_mask, workload_overall,
        forgive_intensity=True, ambient_median=ambient_median,
    )
    v0["label"] = "V0 legacy"
    v0["gate"] = (
        "Pre-V5 production: temp 0.70 + rpm 0.30, weights cpu 4/7 / gpu 3/7 / vrm 1/7, "
        "current absolute gate (CPU>=10% or GPU>=30%), with -10C * intensity forgiveness."
    )

    v1_mask = _ge(cpu_load_col, 70.0) | _ge(gpu_load_col, 70.0)
    v1 = _score_with_mask(
        data, stats, v1_mask, workload_overall,
        forgive_intensity=True, ambient_median=ambient_median,
    )
    v1["label"] = "V1 tight"
    v1["gate"] = "Tight load gate (CPU>=70% or GPU>=70%); legacy weights and forgiveness."

    v2_mask = _per_sample_intensity_mask(data, stats, threshold=_V2_INTENSITY_THRESHOLD)
    v2 = _score_with_mask(
        data, stats, v2_mask, workload_overall,
        forgive_intensity=True, ambient_median=ambient_median,
    )
    v2["label"] = "V2 intensity"
    v2["gate"] = (
        "Per-sample workload intensity (clock/Vcore/W blend) >= "
        f"{_V2_INTENSITY_THRESHOLD}; legacy weights and forgiveness."
    )

    v3 = _score_with_mask(
        data, stats, legacy_mask, workload_overall,
        forgive_intensity=False, ambient_median=ambient_median,
    )
    v3["label"] = "V3 no-forgive"
    v3["gate"] = "Current gate, legacy weights, but workload-intensity forgiveness removed."

    v4_score = workload.score if workload and not np.isnan(workload.score) else float("nan")
    v4 = {
        "score": v4_score,
        "temp_score": float("nan"),
        "rpm_score": float("nan"),
        "weighted_rise": float("nan"),
        "cpu_rise": float("nan"),
        "gpu_rise": float("nan"),
        "vrm_rise": float("nan"),
        "high_total_rpm": float("nan"),
        "n_samples": 0,
        "n_total": int(len(data.df.index)),
        "coverage": float("nan"),
        "label": "V4 intensity*",
        "gate": (
            "WorkloadIntensity score (clocks/Vcore/W blend, 0..100). NOT a cooling "
            "score - listed to confirm captures are under similar silicon pressure."
        ),
    }

    v5 = _score_v5_full(data, stats, ambient_median)
    v5["label"] = "V5 production"
    v5["gate"] = (
        "Per-component matched-quantile masks (top "
        f"{int(_V5_QUANTILE * 100)}% of own samples), "
        f"weights cpu={_V5_CPU_W} / gpu={_V5_GPU_W} / vrm={_V5_VRM_W}, "
        f"temp={_V5_TEMP_W} / rpm={_V5_RPM_W}; idle components "
        f"(70th-pct load < {_V5_MIN_LOAD_FLOOR:g}%) dropped from the temp term."
    )

    return {"V0": v0, "V1": v1, "V2": v2, "V3": v3, "V4": v4, "V5": v5}


# ---------- public ------------------------------------------------------------


_HEADLINE_HOTSPOTS = ["cpu_pkg", "gpu_hotspot", "gpu_core", "vrm"]


def compute_cooling_kpis(
    data: ConfigData, stats: PerConfigStats
) -> CoolingKPIs:
    """Compute the full cooling KPI bundle for one config."""
    system_col = stats.primary_temps.get("system")
    ambient_median = (
        _median_numeric(data.df[system_col])
        if system_col and system_col in data.df.columns
        else float("nan")
    )

    cpu_high = _compute_high_load_mask(data, stats, "cpu_total")
    gpu_high = _compute_high_load_mask(data, stats, "gpu_core")
    high_mask = cpu_high | gpu_high
    high_fraction = float(high_mask.mean()) if len(high_mask) else 0.0

    median_total_rpm = _total_fan_rpm(data, stats, None)
    high_total_rpm = _total_fan_rpm(data, stats, high_mask if high_mask.any() else None)

    hotspots: list[HotspotKPI] = []
    for key in _HEADLINE_HOTSPOTS:
        if key not in stats.primary_temps:
            continue
        kpi = _hotspot_kpi(data, stats, key, _label_for(key), ambient_median)
        if kpi is not None:
            hotspots.append(kpi)

    workload = _compute_workload_intensity(data, stats, high_mask)

    # Composite cooling score (V5).
    # See ``_score_v5_full`` and the module docstring for the full definition.
    # Summary: each config is scored on its own top-30% busiest CPU samples
    # for CPU rise, top-30% busiest GPU samples for GPU rise, and the union of
    # the two for VRM rise and total fan RPM. Weights cpu=0.55 / gpu=0.30 /
    # vrm=0.15 inside the temperature term; composite = 0.80 * temp + 0.20 * rpm.
    # No workload-intensity forgiveness (the matched-quantile gate handles
    # different workload distributions across configs more cleanly). Components
    # whose 70th-percentile load is < 5% are treated as idle and dropped, so a
    # capture with no GPU work doesn't get a bogus "0 GPU rise" credit.
    #
    # If V5 cannot produce a score (all components idle for this capture), we
    # fall back to the legacy V0 formula so we never regress to "no score".
    v5_full = _score_v5_full(data, stats, ambient_median)
    cooling_score = float(v5_full["score"]) if not np.isnan(float(v5_full["score"])) else float("nan")
    cooling_score_is_v5 = not np.isnan(cooling_score)
    if not cooling_score_is_v5:
        legacy = _score_with_mask(
            data, stats, high_mask,
            workload.overall if workload else float("nan"),
            forgive_intensity=True, ambient_median=ambient_median,
        )
        cooling_score = float(legacy["score"])

    return CoolingKPIs(
        name=data.name,
        ambient_median=ambient_median,
        high_load_mask_fraction=high_fraction,
        median_total_fan_rpm=median_total_rpm,
        high_load_total_fan_rpm=high_total_rpm,
        cooling_score=cooling_score,
        cooling_score_is_v5=cooling_score_is_v5,
        workload=workload,
        hotspots=hotspots,
    )


def rank_configs(kpis: list[CoolingKPIs]) -> list[tuple[str, float]]:
    """Return configs ranked by ``cooling_score`` descending (winner first).

    Configs whose score came from the V0 fallback (capture too idle for V5
    matched-quantile gating) are excluded from the ranking - their score is
    not directly comparable to V5-scored configs. They still appear in the
    summary table with their fallback score, but no verdict is rendered for
    them.
    """
    scored = [
        (k.name, k.cooling_score)
        for k in kpis
        if not np.isnan(k.cooling_score) and k.cooling_score_is_v5
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


# ---------- HTML rendering helpers -------------------------------------------


def kpis_to_summary_rows(kpis: list[CoolingKPIs]) -> list[dict[str, object]]:
    """Flatten KPI bundles into rows for the headline table."""
    rows: list[dict[str, object]] = []
    for k in kpis:
        # Pull CPU and GPU headline numbers if present.
        per_hot = {h.hotspot_key: h for h in k.hotspots}
        cpu = per_hot.get("cpu_pkg")
        gpu = per_hot.get("gpu_hotspot") or per_hot.get("gpu_core")
        w = k.workload
        rows.append(
            {
                "name": k.name,
                "cooling_score": k.cooling_score,
                "cooling_score_is_v5": k.cooling_score_is_v5,
                "ambient_median": k.ambient_median,
                "cpu_high_load_median": cpu.high_load_median if cpu else float("nan"),
                "cpu_rise": cpu.temp_rise_over_ambient if cpu else float("nan"),
                "cpu_c_per_w": cpu.c_per_watt if cpu else float("nan"),
                "cpu_headroom": cpu.thermal_headroom if cpu else float("nan"),
                "gpu_high_load_median": gpu.high_load_median if gpu else float("nan"),
                "gpu_rise": gpu.temp_rise_over_ambient if gpu else float("nan"),
                "gpu_c_per_w": gpu.c_per_watt if gpu else float("nan"),
                "gpu_headroom": gpu.thermal_headroom if gpu else float("nan"),
                "median_fan_rpm_total": k.median_total_fan_rpm,
                "high_load_fan_rpm_total": k.high_load_total_fan_rpm,
                "high_load_fraction": k.high_load_mask_fraction,
                "workload_score": w.score if w else float("nan"),
                "workload_cpu_intensity": (w.cpu_intensity * 100.0) if (w and not np.isnan(w.cpu_intensity)) else float("nan"),
                "workload_gpu_intensity": (w.gpu_intensity * 100.0) if (w and not np.isnan(w.gpu_intensity)) else float("nan"),
                "workload_cpu_clock_ghz": w.cpu_clock_ghz if w else float("nan"),
                "workload_cpu_vcore_v": w.cpu_vcore_v if w else float("nan"),
                "workload_cpu_pkg_w": w.cpu_package_w if w else float("nan"),
                "workload_gpu_clock_mhz": w.gpu_clock_mhz if w else float("nan"),
                "workload_gpu_voltage_v": w.gpu_voltage_v if w else float("nan"),
                "workload_gpu_pkg_w": w.gpu_power_w if w else float("nan"),
            }
        )
    return rows


def kpis_to_detail_rows(kpis: list[CoolingKPIs]) -> list[dict[str, object]]:
    """One row per (config, hotspot) for the detailed KPI table."""
    rows: list[dict[str, object]] = []
    for k in kpis:
        for h in k.hotspots:
            rows.append(
                {
                    "config": k.name,
                    "hotspot": h.hotspot_label,
                    "median": h.median_temp,
                    "high_load_median": h.high_load_median,
                    "p95": h.p95_temp,
                    "rise_over_ambient": h.temp_rise_over_ambient,
                    "c_per_watt": h.c_per_watt,
                    "median_power_w": h.median_power_w,
                    "headroom": h.thermal_headroom,
                }
            )
    return rows
