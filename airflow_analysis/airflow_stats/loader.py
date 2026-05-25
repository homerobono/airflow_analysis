"""Discover ``Config X/`` folders and load their LibreHardwareMonitor CSV logs.

LHM CSVs have a two-row header:

* row 1: sensor path, e.g. ``/lpc/nct6687dr/0/fan/0`` (globally unique)
* row 2: friendly label, e.g. ``"CPU Fan"`` (not unique)

We key columns by the sensor path and keep the friendly label as metadata so
plots and tables can still display the human-readable name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .setup_md import ConfigSetup, parse_setup_md

_logger = logging.getLogger(__name__)

CONFIG_DIR_RE = re.compile(r"^Config\s+(.+)$", re.IGNORECASE)
CSV_GLOB = "LibreHardwareMonitor*.csv"


@dataclass
class ConfigData:
    """All data loaded for one ``Config X/`` folder."""

    name: str
    root: Path
    setup: ConfigSetup
    df: pd.DataFrame  # index: Time (datetime), columns: sensor paths
    labels: dict[str, str] = field(default_factory=dict)  # sensor_path -> friendly label
    csv_files: list[Path] = field(default_factory=list)
    n_rows: int = 0

    def label(self, sensor_path: str) -> str:
        return self.labels.get(sensor_path, sensor_path)


def _read_one_csv(path: Path) -> tuple[pd.DataFrame, dict[str, str]] | None:
    """Read a single LHM CSV.

    Returns ``(df, labels)`` where ``df`` is indexed by ``Time`` (datetime,
    coerced) and columns are sensor paths, and ``labels`` maps each sensor path
    to its friendly label from header row 2. Returns ``None`` for empty files.
    """
    try:
        # header=[0, 1] gives a MultiIndex; we keep the sensor path (row 0).
        raw = pd.read_csv(path, header=[0, 1], low_memory=False)
    except pd.errors.EmptyDataError:
        _logger.warning("Empty CSV skipped: %s", path)
        return None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Failed to read %s: %s", path, exc)
        return None

    if raw.empty:
        return None

    sensor_paths: list[str] = []
    friendly: list[str] = []
    for col in raw.columns:
        # col is a 2-tuple (sensor_path_or_blank, friendly_label).
        sp, fl = (col[0] if len(col) > 0 else ""), (col[1] if len(col) > 1 else "")
        sp = "" if pd.isna(sp) else str(sp).strip()
        fl = "" if pd.isna(fl) else str(fl).strip()
        sensor_paths.append(sp)
        friendly.append(fl)

    # The Time column has an empty sensor path in row 1; promote it.
    new_cols: list[str] = []
    labels: dict[str, str] = {}
    seen: dict[str, int] = {}
    for sp, fl in zip(sensor_paths, friendly):
        if fl.lower() == "time" or (not sp and fl.lower() == "time"):
            key = "Time"
        elif sp:
            key = sp
        elif fl:
            key = fl
        else:
            key = "__unnamed__"
        # Disambiguate duplicate keys (LHM occasionally repeats sensor paths
        # when a sensor exposes the same metric under multiple labels).
        if key in seen:
            seen[key] += 1
            key = f"{key}#{seen[key]}"
        else:
            seen[key] = 0
        new_cols.append(key)
        if fl and key != "Time":
            labels.setdefault(key, fl)

    df = raw.copy()
    df.columns = new_cols

    if "Time" not in df.columns:
        _logger.warning("No Time column in %s; skipping", path)
        return None

    df["Time"] = pd.to_datetime(df["Time"], errors="coerce", format="%m/%d/%Y %H:%M:%S")
    # Fall back to flexible parser for any rows the strict format missed.
    if df["Time"].isna().any():
        fallback = pd.to_datetime(df["Time"], errors="coerce")
        df["Time"] = df["Time"].fillna(fallback)
    df = df.dropna(subset=["Time"])

    # Coerce all non-Time columns to numeric; non-numeric become NaN.
    for col in df.columns:
        if col == "Time":
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.set_index("Time").sort_index()
    # Drop columns that are entirely NaN within this CSV.
    df = df.dropna(axis=1, how="all")
    return df, labels


# Fan RPM channels we always preserve, even when flat (e.g. an unused header
# reading 0 in this config may carry a real fan in a future config and we
# want the schema to stay aligned across configs).
_FAN_RPM_RE = re.compile(
    r"^/(lpc/[^/]+/\d+|gpu-(?:nvidia|amd)/\d+)/fan/\d+(#\d+)?$",
    re.IGNORECASE,
)


def _drop_near_constant(df: pd.DataFrame, tol: float = 1e-9) -> pd.DataFrame:
    """Remove columns whose std is effectively zero or that are entirely NaN.

    Fan-RPM columns are always preserved even when flat, so disconnected-fan
    headers like ``System Fan #2`` stay in the schema for cross-config
    comparison.
    """
    if df.empty:
        return df
    std = df.std(numeric_only=True)
    moving = std[std.abs() > tol].index.tolist()
    pinned = [c for c in df.columns if _FAN_RPM_RE.match(c)]
    keep = list(dict.fromkeys(moving + pinned))
    return df[keep]


# Fan duty % columns we drop in favor of the matched RPM reading. LHM exposes
# both /lpc/.../control/N (0-100 %) and /lpc/.../fan/N (actual RPM) for the
# same physical fan; the RPM reading is the measured value, the control is
# just the commanded duty cycle and would otherwise be perfectly correlated
# with itself across the channel. We keep RPM and drop control.
_FAN_CONTROL_RE = re.compile(
    r"^/(lpc/[^/]+/\d+|gpu-(?:nvidia|amd)/\d+)/control/\d+(#\d+)?$",
    re.IGNORECASE,
)


def _drop_fan_control_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Drop fan control % columns; keep matching RPM columns.

    Returns the trimmed DataFrame and the list of dropped column names so the
    caller can log them.
    """
    if df.empty:
        return df, []
    dropped = [c for c in df.columns if _FAN_CONTROL_RE.match(c)]
    if not dropped:
        return df, []
    return df.drop(columns=dropped), dropped


def discover_configs(root: Path) -> list[Path]:
    """Return all top-level ``Config X/`` folders sorted alphabetically."""
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and CONFIG_DIR_RE.match(child.name):
            out.append(child)
    return out


def load_config(config_dir: Path) -> ConfigData | None:
    """Load all CSVs under one ``Config X/`` folder (recursively) into one DF."""
    match = CONFIG_DIR_RE.match(config_dir.name)
    name = match.group(1).strip() if match else config_dir.name

    setup_path = config_dir / "setup.md"
    if not setup_path.exists():
        _logger.warning("%s: no setup.md found; using empty setup.", config_dir)
        setup = ConfigSetup(name=name, path=setup_path)
    else:
        setup = parse_setup_md(setup_path, name)

    csv_files = sorted(p for p in config_dir.rglob(CSV_GLOB) if p.is_file())
    if not csv_files:
        _logger.warning("%s: no CSV files found, skipping.", config_dir)
        return None

    frames: list[pd.DataFrame] = []
    labels: dict[str, str] = {}
    for path in csv_files:
        result = _read_one_csv(path)
        if result is None:
            continue
        df, lbls = result
        frames.append(df)
        for k, v in lbls.items():
            labels.setdefault(k, v)

    if not frames:
        _logger.warning("%s: no readable CSVs.", config_dir)
        return None

    big = pd.concat(frames, axis=0, join="outer", sort=False).sort_index()
    big = big[~big.index.duplicated(keep="first")]

    big, dropped_ctrl = _drop_fan_control_columns(big)
    if dropped_ctrl:
        _logger.info(
            "%s: dropped %d fan control %% columns (kept matching RPM).",
            config_dir, len(dropped_ctrl),
        )

    before = big.shape[1]
    big = _drop_near_constant(big)
    dropped = before - big.shape[1]
    if dropped:
        _logger.info("%s: dropped %d near-constant columns.", config_dir, dropped)

    # Replace any remaining ±inf with NaN for safety.
    big = big.replace([np.inf, -np.inf], np.nan)

    return ConfigData(
        name=name,
        root=config_dir,
        setup=setup,
        df=big,
        labels={k: v for k, v in labels.items() if k in big.columns},
        csv_files=csv_files,
        n_rows=len(big),
    )


def load_all(root: Path) -> list[ConfigData]:
    """Load every ``Config X/`` folder under ``root``."""
    configs: list[ConfigData] = []
    for cdir in discover_configs(root):
        data = load_config(cdir)
        if data is not None:
            configs.append(data)
            _logger.info(
                "Loaded Config %s: %d rows, %d sensors, %d CSVs.",
                data.name, data.n_rows, data.df.shape[1], len(data.csv_files),
            )
    return configs
