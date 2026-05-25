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
    """Scatter + LOWESS curve of y vs x."""
    x = pd.to_numeric(x, errors="coerce")
    y = pd.to_numeric(y, errors="coerce")
    joined = pd.concat([x, y], axis=1).dropna()
    if len(joined) < 20:
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
    """Heatmap of a (bin x sensor) matrix."""
    if matrix is None or matrix.empty:
        return None
    df = matrix.drop(columns=[c for c in matrix.columns if c == "__n__"], errors="ignore")
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
