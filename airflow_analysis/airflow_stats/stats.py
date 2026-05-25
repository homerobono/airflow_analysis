"""Per-config statistical analyses."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .loader import ConfigData
from .schema import SensorSchema, classify, primary_loads, primary_temps

# Decile edges for load binning (percent units, 0..100).
LOAD_BIN_EDGES = np.array([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 101], dtype=float)
LOAD_BIN_LABELS = [
    "0-10", "10-20", "20-30", "30-40", "40-50",
    "50-60", "60-70", "70-80", "80-90", "90-100",
]


@dataclass
class PerConfigStats:
    name: str
    schema: SensorSchema
    primary_temps: dict[str, str]
    primary_loads: dict[str, str]
    summary: pd.DataFrame                       # index: sensor path
    corr_spearman: pd.DataFrame                 # square matrix
    corr_pearson: pd.DataFrame                  # square matrix
    load_binned_cpu: pd.DataFrame = field(default_factory=pd.DataFrame)
    load_binned_gpu: pd.DataFrame = field(default_factory=pd.DataFrame)
    lag_table: pd.DataFrame = field(default_factory=pd.DataFrame)
    sample_period_s: float = float("nan")


# ---------- helpers -----------------------------------------------------------


def _summary_stats(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """mean / median / std / p05 / p95 / min / max / n for each column."""
    if not cols:
        return pd.DataFrame()
    sub = df[cols].apply(pd.to_numeric, errors="coerce")
    out = pd.DataFrame(
        {
            "n": sub.count(),
            "mean": sub.mean(),
            "median": sub.median(),
            "std": sub.std(),
            "p05": sub.quantile(0.05),
            "p95": sub.quantile(0.95),
            "min": sub.min(),
            "max": sub.max(),
        }
    )
    return out


def _correlations(df: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not cols:
        empty = pd.DataFrame()
        return empty, empty
    sub = df[cols].apply(pd.to_numeric, errors="coerce")
    # Drop columns that are still constant after numeric coercion.
    std = sub.std()
    keep = std[std > 0].index.tolist()
    sub = sub[keep]
    if sub.shape[1] < 2:
        empty = pd.DataFrame()
        return empty, empty
    spearman = sub.corr(method="spearman")
    pearson = sub.corr(method="pearson")
    return spearman, pearson


def _bin_by_load(
    df: pd.DataFrame,
    load_col: str,
    target_cols: list[str],
) -> pd.DataFrame:
    """Median of each ``target_col`` within decile bins of ``load_col``.

    Returns a DataFrame indexed by bin label with one column per target.
    """
    if load_col not in df.columns or not target_cols:
        return pd.DataFrame()
    series = pd.to_numeric(df[load_col], errors="coerce")
    bins = pd.cut(
        series,
        bins=LOAD_BIN_EDGES,
        labels=LOAD_BIN_LABELS,
        include_lowest=True,
        right=False,
    )
    sub = df[target_cols].apply(pd.to_numeric, errors="coerce")
    grouped = sub.groupby(bins, observed=False).median()
    counts = sub.groupby(bins, observed=False).size()
    grouped["__n__"] = counts
    return grouped


def _sample_period_seconds(df: pd.DataFrame) -> float:
    if df.empty or len(df) < 2:
        return float("nan")
    deltas = df.index.to_series().diff().dropna().dt.total_seconds()
    deltas = deltas[(deltas > 0) & (deltas < 60)]  # ignore session gaps
    if deltas.empty:
        return float("nan")
    return float(deltas.median())


def _lag_xcorr(
    a: pd.Series, b: pd.Series, max_lag: int
) -> tuple[int, float]:
    """Argmax cross-correlation of a(t-k) vs b(t) over k in [0, max_lag].

    Returns ``(best_lag, best_corr)``. Returns ``(0, nan)`` if not computable.
    """
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    n = min(len(a), len(b))
    if n < max_lag + 10:
        return 0, float("nan")
    best_lag = 0
    best_corr = float("nan")
    for k in range(0, max_lag + 1):
        if k == 0:
            x, y = a, b
        else:
            x, y = a.iloc[:-k], b.iloc[k:]
        if len(x) != len(y):
            continue
        joined = pd.concat([x.reset_index(drop=True), y.reset_index(drop=True)], axis=1).dropna()
        if len(joined) < 30 or joined.iloc[:, 0].std() == 0 or joined.iloc[:, 1].std() == 0:
            continue
        c = joined.iloc[:, 0].corr(joined.iloc[:, 1])
        if pd.isna(c):
            continue
        if pd.isna(best_corr) or c > best_corr:
            best_corr = float(c)
            best_lag = k
    return best_lag, best_corr


def _build_lag_table(
    df: pd.DataFrame,
    schema: SensorSchema,
    p_loads: dict[str, str],
    p_temps: dict[str, str],
    sample_period_s: float,
) -> pd.DataFrame:
    """For each (load, temp) pair, find lag (sec) that maximizes correlation."""
    if df.empty or not p_loads or not p_temps:
        return pd.DataFrame()
    # cap lag at ~5 minutes of samples
    if sample_period_s and not np.isnan(sample_period_s):
        max_lag = int(min(600, max(5, 300 / sample_period_s)))
    else:
        max_lag = 60

    rows: list[dict[str, float | str]] = []
    for load_key, load_col in p_loads.items():
        if load_col not in df.columns:
            continue
        for temp_key, temp_col in p_temps.items():
            if temp_col not in df.columns:
                continue
            lag, corr = _lag_xcorr(df[load_col], df[temp_col], max_lag)
            rows.append(
                {
                    "load": load_key,
                    "temp": temp_key,
                    "best_lag_samples": lag,
                    "best_lag_seconds": (
                        lag * sample_period_s
                        if sample_period_s and not np.isnan(sample_period_s)
                        else float("nan")
                    ),
                    "best_corr": corr,
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ---------- public ------------------------------------------------------------


def analyze_config(data: ConfigData) -> PerConfigStats:
    """Compute every per-config statistic used by the report."""
    df = data.df
    schema = classify(df)

    # Pull headline sensors.
    p_temps = primary_temps(df, data.labels, schema)
    p_loads = primary_loads(df, data.labels, schema)

    # Summary across all "interesting" columns (temps + fans + loads + power).
    interesting = (
        schema.temps()
        + schema.fans_rpm()
        + schema.fans_pct()
        + schema.loads()
        + schema.power()
    )
    summary = _summary_stats(df, interesting)
    if not summary.empty:
        summary.insert(0, "label", [data.label(s) for s in summary.index])

    # Correlations on the same interesting set.
    sp, pe = _correlations(df, interesting)

    sample_period = _sample_period_seconds(df)

    # Load-binned medians for hotspot temps + fan RPMs.
    fans_rpm = schema.fans_rpm()
    targets = list(dict.fromkeys(list(p_temps.values()) + fans_rpm))

    cpu_load_col = p_loads.get("cpu_total")
    gpu_load_col = p_loads.get("gpu_core")
    load_cpu = _bin_by_load(df, cpu_load_col, targets) if cpu_load_col else pd.DataFrame()
    load_gpu = _bin_by_load(df, gpu_load_col, targets) if gpu_load_col else pd.DataFrame()

    lag_table = _build_lag_table(df, schema, p_loads, p_temps, sample_period)

    return PerConfigStats(
        name=data.name,
        schema=schema,
        primary_temps=p_temps,
        primary_loads=p_loads,
        summary=summary,
        corr_spearman=sp,
        corr_pearson=pe,
        load_binned_cpu=load_cpu,
        load_binned_gpu=load_gpu,
        lag_table=lag_table,
        sample_period_s=sample_period,
    )
