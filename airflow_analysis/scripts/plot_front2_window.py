"""Plot a fan's RPM vs a chosen group of temperature sensors over a time window.

Defaults reproduce the original FRONT_2 vs System/CPU/GPU view, but ``--fan``
and ``--temps`` make it reusable (e.g. ``--fan TOP_1 --temps cpu``).
"""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from airflow_stats.loader import load_config
from airflow_stats.schema import classify, primary_temps


def _resolve_fan_column(data, slot_name: str) -> str | None:
    """Find the RPM sensor path for a physical slot (e.g. ``FRONT_2``)."""
    inv = {slot: friendly for friendly, slot in data.setup.fan_map.items()}
    friendly = inv.get(slot_name)
    if friendly is None:
        return None
    candidates = [
        sp for sp, lbl in data.labels.items()
        if lbl == friendly and "/fan/" in sp
    ]
    return candidates[0] if candidates else None


def _pick_temp_columns(data, df, temps_group: str) -> list[tuple[str, str, str]]:
    """Return ``[(sensor_path, display_label, color), ...]`` for the chosen group.

    Groups:
      * ``default`` : System + CPU pkg + GPU hotspot (one line each).
      * ``cpu``     : every CPU temperature sensor present (Tctl, CCD1, cores).
      * ``gpu``     : every GPU temperature sensor present.
      * ``mb``      : motherboard temps (System, VRM MOS, Chipset, CPU Core ...).
    """
    schema = classify(df)
    if temps_group == "default":
        temps = primary_temps(df, data.labels, schema)
        triples: list[tuple[str, str, str]] = []
        for key, color in [("system", "tab:green"),
                           ("cpu_pkg", "tab:red"),
                           ("gpu_hotspot", "tab:purple")]:
            col = temps.get(key)
            if col is None and key == "gpu_hotspot":
                col = temps.get("gpu_core")
            if col is not None:
                triples.append((col, data.labels.get(col, col), color))
        return triples

    if temps_group == "cpu":
        cols = list(schema.cpu_temps)
    elif temps_group == "gpu":
        cols = list(schema.gpu_temps)
    elif temps_group == "mb":
        cols = list(schema.mb_temps)
    else:
        raise SystemExit(f"Unknown --temps group: {temps_group}")

    cmap = plt.get_cmap("tab20" if len(cols) > 10 else "tab10")
    out: list[tuple[str, str, str]] = []
    for i, col in enumerate(cols):
        out.append((col, data.labels.get(col, col), cmap(i % cmap.N)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="C")
    ap.add_argument("--fan", default="FRONT_2",
                    help="Physical slot name from setup.md fan_map (e.g. FRONT_2, TOP_1).")
    ap.add_argument("--temps", default="default",
                    choices=["default", "cpu", "gpu", "mb"])
    ap.add_argument("--start", default=None, help="ISO datetime")
    ap.add_argument("--end", default=None, help="ISO datetime")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    config_dir = repo_root / f"Config {args.config}"
    data = load_config(config_dir)
    if data is None:
        raise SystemExit(f"No data loaded from {config_dir}")

    if args.start and args.end:
        start = datetime.fromisoformat(args.start)
        end = datetime.fromisoformat(args.end)
    else:
        today = datetime.now().date()
        start = datetime.combine(today - timedelta(days=1), time(15, 0))
        end = datetime.combine(today, time(1, 0))

    df = data.df.loc[start:end]
    if df.empty:
        raise SystemExit(f"No samples in window {start} .. {end}")

    fan_col = _resolve_fan_column(data, args.fan)
    if fan_col is None or fan_col not in df.columns:
        raise SystemExit(f"Could not resolve {args.fan} fan RPM column.")

    temp_triples = _pick_temp_columns(data, df, args.temps)
    if not temp_triples:
        raise SystemExit(f"No temperature columns found for group '{args.temps}'.")

    fig, ax_t = plt.subplots(figsize=(13, 6))

    for col, label, color in temp_triples:
        ax_t.plot(df.index, df[col], lw=1.1, color=color, label=label)
    ax_t.set_ylabel("Temperature (°C)")
    ax_t.grid(True, alpha=0.3)

    ax_r = ax_t.twinx()
    ax_r.plot(df.index, df[fan_col], color="black", lw=1.0, alpha=0.85,
              label=f"{args.fan} RPM  ({data.labels.get(fan_col, fan_col)})")
    ax_r.set_ylabel(f"{args.fan} RPM")

    ax_t.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax_t.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()

    lines, labels = ax_t.get_legend_handles_labels()
    lines2, labels2 = ax_r.get_legend_handles_labels()
    ax_t.legend(lines2 + lines, labels2 + labels, loc="upper left", fontsize=8, ncol=2)

    ax_t.set_title(
        f"Config {args.config} — {args.fan} RPM vs {args.temps} temps\n"
        f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}  ({len(df)} samples)"
    )

    out_path = Path(args.out) if args.out else (
        repo_root / "airflow_analysis"
        / f"{args.fan.lower()}_rpm_vs_{args.temps}_"
          f"{start:%Y%m%d-%H%M}_{end:%Y%m%d-%H%M}.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    print(f"Wrote {out_path}")
    print(f"  fan : {fan_col}  ({data.labels.get(fan_col)})")
    for col, label, _ in temp_triples:
        print(f"  temp: {col}  ({label})")


if __name__ == "__main__":
    main()
