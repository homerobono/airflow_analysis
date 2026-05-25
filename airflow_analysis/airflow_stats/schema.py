"""Classify LHM sensor-path columns into semantic groups.

Sensor paths follow a stable convention, e.g. ``/lpc/.../fan/3``,
``/amdcpu/0/temperature/2``, ``/gpu-nvidia/0/load/0``. We use regex to bucket
columns; everything else falls into ``other``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

# ---------- patterns ----------------------------------------------------------

# Motherboard (SuperIO) groups.
_MB_TEMP = re.compile(r"^/lpc/[^/]+/\d+/temperature/\d+$", re.IGNORECASE)
_MB_FAN_RPM = re.compile(r"^/lpc/[^/]+/\d+/fan/\d+$", re.IGNORECASE)
_MB_FAN_PCT = re.compile(r"^/lpc/[^/]+/\d+/control/\d+$", re.IGNORECASE)

# CPU.
_CPU_TEMP = re.compile(r"^/(amd|intel)cpu/\d+/temperature/\d+$", re.IGNORECASE)
_CPU_LOAD = re.compile(r"^/(amd|intel)cpu/\d+/load/\d+$", re.IGNORECASE)
_CPU_POWER = re.compile(r"^/(amd|intel)cpu/\d+/power/\d+$", re.IGNORECASE)
_CPU_CLOCK = re.compile(r"^/(amd|intel)cpu/\d+/clock/\d+$", re.IGNORECASE)

# GPU (covers nvidia and amd).
_GPU_TEMP = re.compile(r"^/gpu-(nvidia|amd)/\d+/temperature/\d+$", re.IGNORECASE)
_GPU_LOAD = re.compile(r"^/gpu-(nvidia|amd)/\d+/load/\d+$", re.IGNORECASE)
_GPU_POWER = re.compile(r"^/gpu-(nvidia|amd)/\d+/power/\d+$", re.IGNORECASE)
_GPU_FAN_RPM = re.compile(r"^/gpu-(nvidia|amd)/\d+/fan/\d+$", re.IGNORECASE)
_GPU_FAN_PCT = re.compile(r"^/gpu-(nvidia|amd)/\d+/control/\d+$", re.IGNORECASE)

# Memory / RAM.
_RAM_TEMP = re.compile(r"^/memory/dimm/\d+/temperature/\d+$", re.IGNORECASE)

# Storage.
_STORAGE_TEMP = re.compile(r"^/(nvme|ssd|hdd)/\d+/temperature/\d+$", re.IGNORECASE)


@dataclass
class SensorSchema:
    """Bucketed sensor lists for one config's DataFrame.

    Each list contains sensor-path column names that exist in the DataFrame.
    """

    all_columns: list[str] = field(default_factory=list)

    mb_temps: list[str] = field(default_factory=list)
    mb_fans_rpm: list[str] = field(default_factory=list)
    mb_fans_pct: list[str] = field(default_factory=list)

    cpu_temps: list[str] = field(default_factory=list)
    cpu_loads: list[str] = field(default_factory=list)
    cpu_power: list[str] = field(default_factory=list)
    cpu_clocks: list[str] = field(default_factory=list)

    gpu_temps: list[str] = field(default_factory=list)
    gpu_loads: list[str] = field(default_factory=list)
    gpu_power: list[str] = field(default_factory=list)
    gpu_fans_rpm: list[str] = field(default_factory=list)
    gpu_fans_pct: list[str] = field(default_factory=list)

    ram_temps: list[str] = field(default_factory=list)
    storage_temps: list[str] = field(default_factory=list)

    other: list[str] = field(default_factory=list)

    # ---- convenience views -------------------------------------------------

    def temps(self) -> list[str]:
        return [
            *self.mb_temps,
            *self.cpu_temps,
            *self.gpu_temps,
            *self.ram_temps,
            *self.storage_temps,
        ]

    def fans_rpm(self) -> list[str]:
        return [*self.mb_fans_rpm, *self.gpu_fans_rpm]

    def fans_pct(self) -> list[str]:
        return [*self.mb_fans_pct, *self.gpu_fans_pct]

    def loads(self) -> list[str]:
        return [*self.cpu_loads, *self.gpu_loads]

    def power(self) -> list[str]:
        return [*self.cpu_power, *self.gpu_power]


def classify(df: pd.DataFrame) -> SensorSchema:
    """Return a SensorSchema for the columns present in ``df``."""
    schema = SensorSchema(all_columns=list(df.columns))
    for col in df.columns:
        if _MB_TEMP.match(col):
            schema.mb_temps.append(col)
        elif _MB_FAN_RPM.match(col):
            schema.mb_fans_rpm.append(col)
        elif _MB_FAN_PCT.match(col):
            schema.mb_fans_pct.append(col)
        elif _CPU_TEMP.match(col):
            schema.cpu_temps.append(col)
        elif _CPU_LOAD.match(col):
            schema.cpu_loads.append(col)
        elif _CPU_POWER.match(col):
            schema.cpu_power.append(col)
        elif _CPU_CLOCK.match(col):
            schema.cpu_clocks.append(col)
        elif _GPU_TEMP.match(col):
            schema.gpu_temps.append(col)
        elif _GPU_LOAD.match(col):
            schema.gpu_loads.append(col)
        elif _GPU_POWER.match(col):
            schema.gpu_power.append(col)
        elif _GPU_FAN_RPM.match(col):
            schema.gpu_fans_rpm.append(col)
        elif _GPU_FAN_PCT.match(col):
            schema.gpu_fans_pct.append(col)
        elif _RAM_TEMP.match(col):
            schema.ram_temps.append(col)
        elif _STORAGE_TEMP.match(col):
            schema.storage_temps.append(col)
        else:
            schema.other.append(col)
    return schema


# ---- "primary" sensor selection ---------------------------------------------

# Heuristics to pick the headline sensor in each category, by friendly label.
# We match against the lowercased friendly label.
_HOTSPOT_HINTS: dict[str, list[str]] = {
    "cpu_pkg": ["core (tctl/tdie)", "cpu package", "package", "cpu cores", "cpu total"],
    "cpu_ccd1": ["ccd1 (tdie)", "ccd1"],
    "gpu_core": ["gpu core"],
    "gpu_hotspot": ["gpu hot spot", "hot spot", "hotspot"],
    "gpu_mem_junction": ["gpu memory junction", "memory junction"],
    "system": ["system"],
    "vrm": ["vrm mos", "vrm"],
    "chipset": ["chipset"],
    "dimm1": ["dimm #1"],
    "dimm3": ["dimm #3"],
}

_LOAD_HINTS: dict[str, list[str]] = {
    "cpu_total": ["cpu total"],
    "gpu_core": ["gpu core"],
}


def _pick_by_label(
    df: pd.DataFrame,
    labels: dict[str, str],
    candidates: list[str],
    hints: list[str],
) -> str | None:
    """Find a sensor whose friendly label contains any of the hint substrings.

    Prefers shorter labels (less likely to be a derived/aggregate variant).
    """
    pool = [c for c in candidates if c in df.columns]
    matches: list[tuple[int, str]] = []
    for sensor in pool:
        lbl = labels.get(sensor, "").lower()
        for hint in hints:
            if hint in lbl:
                matches.append((len(lbl), sensor))
                break
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def primary_temps(
    df: pd.DataFrame, labels: dict[str, str], schema: SensorSchema
) -> dict[str, str]:
    """Pick one canonical sensor per hotspot category."""
    out: dict[str, str] = {}
    pools: dict[str, list[str]] = {
        "cpu_pkg": schema.cpu_temps,
        "cpu_ccd1": schema.cpu_temps,
        "gpu_core": schema.gpu_temps,
        "gpu_hotspot": schema.gpu_temps,
        "gpu_mem_junction": schema.gpu_temps,
        "system": schema.mb_temps,
        "vrm": schema.mb_temps,
        "chipset": schema.mb_temps,
        "dimm1": schema.ram_temps,
        "dimm3": schema.ram_temps,
    }
    for key, hints in _HOTSPOT_HINTS.items():
        pick = _pick_by_label(df, labels, pools[key], hints)
        if pick is not None:
            out[key] = pick
    return out


def primary_loads(
    df: pd.DataFrame, labels: dict[str, str], schema: SensorSchema
) -> dict[str, str]:
    """Pick canonical CPU/GPU load sensors."""
    out: dict[str, str] = {}
    for key, hints in _LOAD_HINTS.items():
        pool = schema.cpu_loads if key == "cpu_total" else schema.gpu_loads
        pick = _pick_by_label(df, labels, pool, hints)
        if pick is not None:
            out[key] = pick
    return out
