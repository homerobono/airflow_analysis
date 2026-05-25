"""Matplotlib/seaborn helpers that return base64-encoded PNGs.

All plots are sized for embedding in an HTML report and use a consistent theme.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import tempfile
from typing import Iterable

# Direct matplotlib's cache to a writable temp dir if the default isn't
# accessible (e.g. sandboxed runs where $HOME is read-only).
os.environ.setdefault(
    "MPLCONFIGDIR",
    os.path.join(tempfile.gettempdir(), "airflow_stats_mpl"),
)
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

import matplotlib

matplotlib.use("Agg")  # headless

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402

_logger = logging.getLogger(__name__)

sns.set_theme(style="whitegrid", context="notebook")


def _fig_to_b64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def violin_temps(
    series_by_label: dict[str, pd.Series], title: str
) -> str | None:
    """One violin per sensor, distribution of temperature values."""
    rows: list[dict] = []
    for label, s in series_by_label.items():
        s = pd.to_numeric(s, errors="coerce").dropna()
        if s.empty:
            continue
        for v in s.to_numpy():
            rows.append({"sensor": label, "value": float(v)})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(max(6, len(series_by_label) * 1.1), 4.5))
    sns.violinplot(data=df, x="sensor", y="value", inner="quartile", ax=ax, cut=0)
    ax.set_title(title)
    ax.set_xlabel("")
    ax.set_ylabel("Temperature (C)")
    ax.tick_params(axis="x", rotation=30)
    return _fig_to_b64(fig)


def lowess_scatter(
    x: pd.Series, y: pd.Series, x_label: str, y_label: str, title: str
) -> str | None:
    """Scatter + LOWESS curve of y vs x.

    Returns ``None`` (and the caller silently skips the plot) when either
    series is constant - typically because a fan header isn't physically
    connected and reads 0 the whole time.
    """
    x = pd.to_numeric(x, errors="coerce")
    y = pd.to_numeric(y, errors="coerce")
    joined = pd.concat([x, y], axis=1).dropna()
    if len(joined) < 20:
        return None
    if joined.iloc[:, 0].std() == 0 or joined.iloc[:, 1].std() == 0:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    sns.regplot(
        x=joined.iloc[:, 0],
        y=joined.iloc[:, 1],
        lowess=True,
        scatter_kws={"alpha": 0.15, "s": 10},
        line_kws={"color": "crimson"},
        ax=ax,
    )
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)
    return _fig_to_b64(fig)


def correlation_heatmap(
    corr: pd.DataFrame,
    labels: dict[str, str],
    title: str,
    min_abs: float = 0.2,
    max_size: int = 35,
) -> str | None:
    """Heatmap of a correlation matrix; column selection filtered for clarity."""
    if corr is None or corr.empty:
        return None
    # Keep columns whose strongest off-diagonal correlation exceeds min_abs.
    mat = corr.copy().astype(float)
    # Build a mask matrix with NaN on the diagonal so we can rank by strongest
    # off-diagonal correlation without mutating the source.
    arr = mat.to_numpy(copy=True)
    np.fill_diagonal(arr, np.nan)
    masked = pd.DataFrame(arr, index=mat.index, columns=mat.columns)
    max_abs = masked.abs().max()
    keep = max_abs[max_abs >= min_abs].index.tolist()
    if len(keep) < 2:
        return None
    if len(keep) > max_size:
        # Keep the strongest movers.
        keep = max_abs.loc[keep].sort_values(ascending=False).head(max_size).index.tolist()
    sub = corr.loc[keep, keep]
    label_map = {k: (labels.get(k, k) or k)[:30] for k in keep}
    sub = sub.rename(index=label_map, columns=label_map)

    n = len(sub)
    fig, ax = plt.subplots(figsize=(max(6, n * 0.45), max(5, n * 0.45)))
    sns.heatmap(
        sub,
        vmin=-1,
        vmax=1,
        center=0,
        cmap="vlag",
        square=True,
        cbar_kws={"shrink": 0.6},
        ax=ax,
    )

    # Paint cells whose correlation is exactly +/-1 black, INCLUDING the
    # diagonal (which is trivially 1 by definition). Off-diagonal black cells
    # flag sensor pairs that carry no extra information beyond each other.
    sub_vals = sub.to_numpy()
    rows, cols = sub_vals.shape
    for i in range(rows):
        for j in range(cols):
            v = sub_vals[i, j]
            if i == j or (pd.notna(v) and abs(abs(float(v)) - 1.0) < 1e-9):
                ax.add_patch(
                    plt.Rectangle((j, i), 1, 1, facecolor="black", edgecolor="none")
                )

    ax.set_title(title)
    ax.tick_params(axis="x", rotation=60)
    ax.tick_params(axis="y", rotation=0)
    return _fig_to_b64(fig)


def load_binned_heatmap(
    matrix: pd.DataFrame,
    title: str,
    cbar_label: str = "C",
    diverging: bool = False,
) -> str | None:
    """Heatmap of a (bin x sensor) matrix.

    Columns whose values are all zero (or all NaN) are dropped from the plot:
    they convey no information and just stretch the colorbar's lower bound.
    """
    if matrix is None or matrix.empty:
        return None
    df = matrix.drop(columns=[c for c in matrix.columns if c == "__n__"], errors="ignore")
    if df.empty:
        return None
    # Drop columns that are entirely zero (treating NaNs as missing, not zero).
    nonzero_cols = [
        c for c in df.columns
        if df[c].dropna().abs().gt(0).any()
    ]
    df = df[nonzero_cols]
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(max(5, df.shape[1] * 0.7 + 2), max(3.5, df.shape[0] * 0.4 + 1)))
    cmap = "vlag" if diverging else "rocket_r"
    center = 0.0 if diverging else None
    sns.heatmap(
        df,
        cmap=cmap,
        center=center,
        annot=True,
        fmt=".1f",
        cbar_kws={"label": cbar_label},
        ax=ax,
    )
    ax.set_title(title)
    ax.set_xlabel("")
    ax.set_ylabel("Load bin (%)")
    ax.tick_params(axis="x", rotation=30)
    return _fig_to_b64(fig)


def grouped_bar(
    df: pd.DataFrame,
    title: str,
    y_label: str,
) -> str | None:
    """Grouped bar chart: rows are categories on x, columns are series."""
    if df is None or df.empty:
        return None
    fig, ax = plt.subplots(figsize=(max(6, df.shape[0] * 0.6 + 2), 4.5))
    df.plot(kind="bar", ax=ax)
    ax.set_title(title)
    ax.set_ylabel(y_label)
    ax.set_xlabel("")
    ax.legend(loc="best", fontsize=8)
    ax.tick_params(axis="x", rotation=30)
    return _fig_to_b64(fig)


def fan_impact_heatmap(
    impact: pd.DataFrame,
    tolerance: pd.DataFrame | None,
    title: str,
    min_tolerance: float = 0.10,
    vmax_abs: float | None = None,
) -> str | None:
    """Heatmap of the fan-impact matrix.

    ``impact`` is degC per +1000 RPM with fans as rows and temperatures as
    columns. Negative cells (cooling) render blue, positive red; cells with
    tolerance below ``min_tolerance`` are overlaid with a hatch pattern to
    flag that the regression couldn't separate that fan from the others.
    """
    if impact is None or impact.empty:
        return None
    df = impact.copy().astype(float)
    if df.dropna(how="all").empty:
        return None

    if vmax_abs is None:
        finite = df.to_numpy()
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return None
        # Symmetric scale so 0 is in the middle of the colorbar.
        vmax_abs = float(max(2.0, np.percentile(np.abs(finite), 95)))

    n_rows, n_cols = df.shape
    fig, ax = plt.subplots(
        figsize=(max(6, n_cols * 1.2 + 2), max(3.5, n_rows * 0.55 + 1.5))
    )
    sns.heatmap(
        df,
        cmap="RdBu_r",
        center=0.0,
        vmin=-vmax_abs,
        vmax=vmax_abs,
        annot=True,
        fmt=".2f",
        cbar_kws={"label": "C per +1000 RPM (lower = more cooling)"},
        ax=ax,
        linewidths=0.4,
        linecolor="white",
    )

    if tolerance is not None and not tolerance.empty:
        # Hatch cells whose fan is collinear with the rest of the regressors.
        # The hatch is drawn as a transparent rectangle layered on top of the
        # heatmap cell.
        tol = tolerance.reindex_like(df)
        for i, fan in enumerate(df.index):
            for j, target in enumerate(df.columns):
                v = df.iat[i, j]
                t = tol.iat[i, j] if (i < tol.shape[0] and j < tol.shape[1]) else np.nan
                if pd.isna(v):
                    ax.add_patch(
                        plt.Rectangle(
                            (j, i), 1, 1,
                            facecolor="#f0f0f0", edgecolor="none",
                        )
                    )
                    continue
                if pd.notna(t) and float(t) < min_tolerance:
                    ax.add_patch(
                        plt.Rectangle(
                            (j, i), 1, 1,
                            fill=False, hatch="////",
                            edgecolor="#444", linewidth=0,
                        )
                    )

    ax.set_title(title)
    ax.set_xlabel("Temperature target")
    ax.set_ylabel("Fan position")
    ax.tick_params(axis="x", rotation=30)
    ax.tick_params(axis="y", rotation=0)
    return _fig_to_b64(fig)


def time_series(
    df: pd.DataFrame, cols: Iterable[str], labels: dict[str, str], title: str
) -> str | None:
    """Down-sampled timeline of selected sensors."""
    cols = [c for c in cols if c in df.columns]
    if not cols or df.empty:
        return None
    # Downsample for plotting performance.
    if len(df) > 5000:
        step = max(1, len(df) // 5000)
        sub = df[cols].iloc[::step]
    else:
        sub = df[cols]
    fig, ax = plt.subplots(figsize=(10, 4))
    for c in cols:
        ax.plot(sub.index, sub[c], label=labels.get(c, c), linewidth=0.8)
    ax.set_title(title)
    ax.set_ylabel("")
    ax.legend(loc="best", fontsize=7, ncol=2)
    fig.autofmt_xdate()
    return _fig_to_b64(fig)
