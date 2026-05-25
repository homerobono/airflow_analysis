"""Fan Impact Matrix.

Answers the question "which fan position affects which temperature" using a
controlled multiple regression instead of raw correlations.

For each target temperature ``T`` we fit:

    T  ~  intercept
        + b_load_cpu  * CPU_load
        + b_load_gpu  * GPU_load
        + b_pow_cpu   * CPU_package_power
        + b_pow_gpu   * GPU_package_power
        + b_amb       * ambient (motherboard "System" sensor)
        + sum_f  b_f * fan_f_RPM

restricted to "warm" samples (CPU load >= 10% OR GPU load >= 30%) so the idle
plateau doesn't dominate the slopes.

The cell of the fan-impact matrix at (fan, temp) is then::

    cell  =  b_f  *  1000          # C per +1000 RPM, controls held fixed

Interpretation::

    cell << 0 : fan RPM up is associated with that temp going DOWN, after
                accounting for load/power/ambient/other fans. Likely useful.
    cell ~ 0  : no measurable thermal effect on that target.
    cell > 0  : fan ramps with temp (controller feedback) faster than it cools
                it. Probably not the right place for this fan, OR collinear
                with another fan (see ``tolerance`` below).

Caveats:

* If two fans always ramp together (same curve, same input), the regression
  CAN'T separate them. We compute a per-fan "tolerance" = 1 - R^2 of that fan
  against the rest of the regressors. Tolerance < ~0.1 means the cell is
  unreliable; the renderer should grey it out or annotate it.
* For the cleanest answer, run sessions where one fan changes at a time. This
  module reports what's recoverable from naturalistic data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .loader import ConfigData
from .stats import PerConfigStats

# High-load gate (mirrors cooling.py).
_HIGH_LOAD_PCT_CPU = 10.0
_HIGH_LOAD_PCT_GPU = 30.0

# Targets we try to fit. Keep them in a stable display order so the matrix
# rows/columns line up across configs.
_TARGET_KEYS: list[str] = [
    "cpu_pkg",
    "gpu_hotspot",
    "gpu_core",
    "gpu_mem_junction",
    "vrm",
    "chipset",
    "dimm1",
    "dimm3",
    "system",
]

_TARGET_LABELS: dict[str, str] = {
    "cpu_pkg": "CPU (Tctl/Tdie)",
    "gpu_hotspot": "GPU Hot Spot",
    "gpu_core": "GPU Core",
    "gpu_mem_junction": "GPU Mem Junction",
    "vrm": "VRM MOS",
    "chipset": "Chipset",
    "dimm1": "DIMM #1",
    "dimm3": "DIMM #3",
    "system": "System (motherboard)",
}

# Minimum sample count to attempt a multivariate fit.
_MIN_N = 50

# Minimum tolerance (1 - R^2 vs other regressors) for a fan coefficient to be
# considered identifiable. Below this, we report the cell but flag it.
_MIN_TOLERANCE = 0.10


@dataclass
class FanImpactMatrix:
    """Per-config impact of each fan on each temperature target."""

    config_name: str

    # Square-ish matrix. Index = fan label, columns = temperature label.
    # Value = degC change per +1000 RPM, controls held fixed.
    impact: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Same shape as ``impact``: 1 - R^2 of that fan against the OTHER
    # regressors (i.e. tolerance / 1-VIF). Lower = the cell is less
    # identifiable from this data.
    tolerance: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Same shape as ``impact``: sample count used for that fit. Identical
    # across columns because the same warm mask is used per target.
    n_samples: pd.DataFrame = field(default_factory=pd.DataFrame)

    # R^2 of each per-target regression (one value per column).
    r2_per_target: pd.Series = field(default_factory=pd.Series)

    # Optional plain-English notes (e.g. "all fans move together; cells are
    # only directional, not absolute").
    notes: list[str] = field(default_factory=list)


# ---------- column pickers ---------------------------------------------------


def _pick_label(
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


def _control_columns(
    data: ConfigData, stats: PerConfigStats
) -> dict[str, str]:
    """Pick the columns we use as regression controls."""
    schema = stats.schema
    out: dict[str, str] = {}

    cpu_load = stats.primary_loads.get("cpu_total")
    if cpu_load and cpu_load in data.df.columns:
        out["cpu_load"] = cpu_load

    gpu_load = stats.primary_loads.get("gpu_core")
    if gpu_load and gpu_load in data.df.columns:
        out["gpu_load"] = gpu_load

    cpu_pkg_power = _pick_label(
        data, schema.cpu_power, ["package", "cpu package", "cpu pkg"]
    )
    if cpu_pkg_power is None and schema.cpu_power:
        cpu_pkg_power = schema.cpu_power[0]
    if cpu_pkg_power and cpu_pkg_power in data.df.columns:
        out["cpu_power"] = cpu_pkg_power

    gpu_pkg_power = _pick_label(
        data, schema.gpu_power, ["package", "gpu power", "board", "total"]
    )
    if gpu_pkg_power is None and schema.gpu_power:
        gpu_pkg_power = schema.gpu_power[0]
    if gpu_pkg_power and gpu_pkg_power in data.df.columns:
        out["gpu_power"] = gpu_pkg_power

    ambient = stats.primary_temps.get("system")
    if ambient and ambient in data.df.columns:
        out["ambient"] = ambient

    return out


def _warm_mask(data: ConfigData, controls: dict[str, str]) -> pd.Series:
    """Restrict the regression to samples where SOMETHING is being asked of the system."""
    df = data.df
    mask = pd.Series(False, index=df.index)
    cpu = controls.get("cpu_load")
    if cpu:
        cs = pd.to_numeric(df[cpu], errors="coerce")
        mask = mask | (cs >= _HIGH_LOAD_PCT_CPU)
    gpu = controls.get("gpu_load")
    if gpu:
        gs = pd.to_numeric(df[gpu], errors="coerce")
        mask = mask | (gs >= _HIGH_LOAD_PCT_GPU)
    if not mask.any():
        # Fall back to the full session - better than refusing to compute.
        mask = pd.Series(True, index=df.index)
    return mask


# ---------- regression -------------------------------------------------------


def _ols_with_diagnostics(
    X: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, float, np.ndarray]:
    """Plain OLS via lstsq; returns (coefs, R^2, tolerance_per_regressor).

    ``X`` already includes the intercept column (first column == all ones).
    Tolerance for regressor j is ``1 - R^2_j`` where R^2_j is from regressing
    column j on the other non-intercept columns. Tolerance == 1 means fully
    independent; tolerance ~ 0 means perfectly collinear with the others.
    The intercept's tolerance is reported as NaN (not a feature).
    """
    coefs, *_ = np.linalg.lstsq(X, y, rcond=None)
    y_hat = X @ coefs
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    n, p = X.shape
    tol = np.full(p, np.nan)
    # Skip intercept (column 0). For each other column, regress it on the rest.
    for j in range(1, p):
        others_idx = [k for k in range(p) if k != j]
        Xj = X[:, others_idx]
        yj = X[:, j]
        if Xj.shape[1] == 0 or yj.std() == 0:
            tol[j] = 1.0
            continue
        c, *_ = np.linalg.lstsq(Xj, yj, rcond=None)
        yj_hat = Xj @ c
        ss_res_j = float(np.sum((yj - yj_hat) ** 2))
        ss_tot_j = float(np.sum((yj - yj.mean()) ** 2))
        r2_j = 1.0 - ss_res_j / ss_tot_j if ss_tot_j > 0 else 0.0
        tol[j] = max(0.0, 1.0 - r2_j)
    return coefs, r2, tol


def _fan_label(data: ConfigData, fan_col: str) -> str:
    return data.labels.get(fan_col, fan_col) or fan_col


def compute_fan_impact(
    data: ConfigData, stats: PerConfigStats
) -> FanImpactMatrix:
    """Compute the fan-impact matrix for one config.

    Returns an empty matrix when there aren't enough fans, targets, or samples
    to fit even one regression.
    """
    df = data.df
    schema = stats.schema
    fan_cols = [c for c in schema.fans_rpm() if c in df.columns]

    # Drop fans that don't actually vary in this session (disconnected
    # headers reading flat zero, or controllers pegged at one speed).
    moving_fans: list[str] = []
    for f in fan_cols:
        s = pd.to_numeric(df[f], errors="coerce").dropna()
        if not s.empty and s.std() > 5.0:  # > 5 RPM variation = real fan
            moving_fans.append(f)
    fan_cols = moving_fans
    if not fan_cols:
        return FanImpactMatrix(config_name=data.name)

    controls = _control_columns(data, stats)
    warm = _warm_mask(data, controls)

    # Build the available targets.
    target_cols: dict[str, str] = {}
    for key in _TARGET_KEYS:
        col = stats.primary_temps.get(key)
        if col and col in df.columns:
            target_cols[key] = col
    if not target_cols:
        return FanImpactMatrix(config_name=data.name)

    fan_labels = [_fan_label(data, f) for f in fan_cols]
    target_labels = [_TARGET_LABELS.get(k, k) for k in target_cols.keys()]

    impact = pd.DataFrame(np.nan, index=fan_labels, columns=target_labels)
    tolerance = pd.DataFrame(np.nan, index=fan_labels, columns=target_labels)
    n_samples = pd.DataFrame(0, index=fan_labels, columns=target_labels, dtype=int)
    r2_per_target = pd.Series(np.nan, index=target_labels, dtype=float)

    notes: list[str] = []
    flagged_target_count = 0

    for tkey, tcol in target_cols.items():
        tlabel = _TARGET_LABELS.get(tkey, tkey)

        # Assemble the regression frame for this target.
        feature_cols: list[tuple[str, str]] = []  # (role, col)
        # Drop ambient as a control when the target IS ambient.
        for role, col in controls.items():
            if role == "ambient" and col == tcol:
                continue
            feature_cols.append((role, col))
        for f in fan_cols:
            feature_cols.append(("fan", f))

        feat_df = df.loc[warm, [tcol] + [c for _, c in feature_cols]].apply(
            pd.to_numeric, errors="coerce"
        ).dropna()
        if len(feat_df) < _MIN_N:
            continue

        y = feat_df[tcol].to_numpy(dtype=float)
        # Drop feature columns that are constant on this slice; they break
        # the linear fit and contribute nothing.
        kept_features: list[tuple[str, str]] = []
        for role, col in feature_cols:
            s = feat_df[col]
            if float(s.std()) > 0:
                kept_features.append((role, col))
        if not any(role == "fan" for role, _ in kept_features):
            # No fan varies for this target's slice; nothing to report.
            continue

        Xmat = feat_df[[c for _, c in kept_features]].to_numpy(dtype=float)
        # Prepend intercept.
        Xmat = np.hstack([np.ones((Xmat.shape[0], 1)), Xmat])

        coefs, r2, tol = _ols_with_diagnostics(Xmat, y)
        r2_per_target.loc[tlabel] = float(r2)

        target_n = int(len(feat_df))
        flagged_this_target = False
        # coefs index 0 = intercept; features start at index 1.
        for k, (role, col) in enumerate(kept_features, start=1):
            if role != "fan":
                continue
            flabel = _fan_label(data, col)
            # ``coefs[k]`` is degC per RPM; multiply by 1000.
            cell = float(coefs[k]) * 1000.0
            impact.loc[flabel, tlabel] = cell
            tolerance.loc[flabel, tlabel] = float(tol[k])
            n_samples.loc[flabel, tlabel] = target_n
            if float(tol[k]) < _MIN_TOLERANCE:
                flagged_this_target = True
        if flagged_this_target:
            flagged_target_count += 1

    if flagged_target_count > 0:
        notes.append(
            f"{flagged_target_count} target(s) had at least one fan with low "
            "tolerance (collinear with other fans). Cells flagged with a hatch "
            "pattern; magnitudes there are not trustworthy. For a clean read, "
            "run a session where only one fan changes at a time."
        )

    if r2_per_target.dropna().empty:
        return FanImpactMatrix(config_name=data.name)

    return FanImpactMatrix(
        config_name=data.name,
        impact=impact,
        tolerance=tolerance,
        n_samples=n_samples,
        r2_per_target=r2_per_target,
        notes=notes,
    )


# ---------- HTML helpers -----------------------------------------------------


def matrix_to_rows(matrix: FanImpactMatrix) -> list[dict[str, object]]:
    """Long-form rows for a tabular HTML rendering.

    One row per (fan, target) cell with non-NaN impact.
    """
    rows: list[dict[str, object]] = []
    if matrix.impact.empty:
        return rows
    for fan in matrix.impact.index:
        for target in matrix.impact.columns:
            v = matrix.impact.loc[fan, target]
            if pd.isna(v):
                continue
            rows.append(
                {
                    "fan": fan,
                    "target": target,
                    "c_per_1000_rpm": float(v),
                    "tolerance": (
                        float(matrix.tolerance.loc[fan, target])
                        if pd.notna(matrix.tolerance.loc[fan, target])
                        else float("nan")
                    ),
                    "n": int(matrix.n_samples.loc[fan, target]),
                }
            )
    return rows


def best_fan_per_target(matrix: FanImpactMatrix) -> dict[str, dict[str, object]]:
    """For each target column, return the most-cooling fan (most-negative slope)
    that has acceptable tolerance.

    Returns a dict keyed by target label; each value has ``fan``, ``c_per_1000_rpm``,
    ``tolerance``, and ``n``. Targets without a usable fan are omitted.
    """
    out: dict[str, dict[str, object]] = {}
    if matrix.impact.empty:
        return out
    for target in matrix.impact.columns:
        col = matrix.impact[target]
        tol = matrix.tolerance[target]
        # Only consider fans with acceptable tolerance and a cooling sign.
        candidates: list[tuple[float, str, float]] = []
        for fan in col.index:
            v = col.loc[fan]
            t = tol.loc[fan]
            if pd.isna(v) or pd.isna(t):
                continue
            if t < _MIN_TOLERANCE:
                continue
            candidates.append((float(v), fan, float(t)))
        if not candidates:
            continue
        candidates.sort()  # most-negative first
        v, fan, t = candidates[0]
        if v >= 0:
            # No fan actually cools this target after controls.
            continue
        out[target] = {
            "fan": fan,
            "c_per_1000_rpm": v,
            "tolerance": t,
            "n": int(matrix.n_samples.loc[fan, target]),
        }
    return out
