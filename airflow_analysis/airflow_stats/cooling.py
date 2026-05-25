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
* ``cooling_score``: composite score combining temperature rise over ambient
  with acoustic cost, normalized to [0, 100] where higher = cooler per
  resource & per RPM. Used to rank configs.
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

    # Composite cooling score.
    # Weights (of the final 100):
    #   - Temperature term (70%): rise of CPU/GPU/VRM over ambient under load.
    #       within: CPU 40% (4/7 of the term), GPU 30% (3/7), VRM 10% (1/7)
    #       i.e. CPU is weighted 10 points more than GPU, VRM gets 10 points.
    #       (rise_under_load = high-load median temp - ambient/System temp)
    #   - Fan term (30%): total fan RPM under load, lower = better.
    # Workload-intensity compensation:
    #   Higher clocks/voltages/power inherently produce more heat and force
    #   higher fan speeds, and that is *not* the airflow's fault. So before
    #   scoring the temperature term we subtract up to 10 C of "forgiven rise"
    #   proportional to the workload intensity (0..1). A pegged-out system
    #   gets +10 C of headroom in the score; an idle run gets 0.
    by_key = {h.hotspot_key: h for h in hotspots}

    def _rise_under_load(key: str) -> float:
        h = by_key.get(key)
        if h is None:
            return float("nan")
        if not np.isnan(h.high_load_median) and not np.isnan(ambient_median):
            return h.high_load_median - ambient_median
        return h.temp_rise_over_ambient

    cpu_rise = _rise_under_load("cpu_pkg")
    gpu_rise = _rise_under_load("gpu_hotspot")
    if np.isnan(gpu_rise):
        gpu_rise = _rise_under_load("gpu_core")
    vrm_rise = _rise_under_load("vrm")

    # Weights are written as fractions of the 70% temperature budget;
    # they sum to 1.0 inside the term so missing sensors don't shrink it.
    component_weights = [
        (cpu_rise, 4.0 / 7.0),   # CPU
        (gpu_rise, 3.0 / 7.0),   # GPU
        (vrm_rise, 1.0 / 7.0),   # VRM
    ]
    available = [(r, w) for r, w in component_weights if not np.isnan(r)]
    if available:
        total_w = sum(w for _, w in available)
        weighted_rise = sum(r * w for r, w in available) / total_w
        # Forgive up to 10 C of rise based on workload intensity.
        intensity = workload.overall if workload and not np.isnan(workload.overall) else 0.0
        adjusted_rise = max(0.0, weighted_rise - 10.0 * intensity)
        # 0 C adjusted rise -> 100, 40 C -> 0.
        temp_score = max(0.0, min(100.0, 100.0 - (adjusted_rise / 40.0) * 100.0))
    else:
        temp_score = float("nan")

    if not np.isnan(high_total_rpm):
        # 0 RPM total -> 100, 8000 RPM total -> ~0.
        rpm_score = max(0.0, min(100.0, 100.0 - (high_total_rpm / 8000.0) * 100.0))
    else:
        rpm_score = float("nan")

    if not np.isnan(temp_score) and not np.isnan(rpm_score):
        cooling_score = 0.70 * temp_score + 0.30 * rpm_score
    elif not np.isnan(temp_score):
        cooling_score = temp_score
    else:
        cooling_score = float("nan")

    return CoolingKPIs(
        name=data.name,
        ambient_median=ambient_median,
        high_load_mask_fraction=high_fraction,
        median_total_fan_rpm=median_total_rpm,
        high_load_total_fan_rpm=high_total_rpm,
        cooling_score=cooling_score,
        workload=workload,
        hotspots=hotspots,
    )


def rank_configs(kpis: list[CoolingKPIs]) -> list[tuple[str, float]]:
    """Return configs ranked by ``cooling_score`` descending (winner first)."""
    scored = [(k.name, k.cooling_score) for k in kpis if not np.isnan(k.cooling_score)]
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
