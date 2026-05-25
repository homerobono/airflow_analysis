"""Command-line entry point for airflow_stats."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .compare import compare_configs
from .loader import load_all
from .report import render_report
from .stats import analyze_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="airflow_stats",
        description="Analyze LibreHardwareMonitor CSV logs to evaluate PC airflow setups.",
    )
    parser.add_argument(
        "root",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Root directory containing 'Config X/' folders (default: cwd).",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("report.html"),
        help="Output HTML file (default: report.html).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    root: Path = args.root.resolve()
    if not root.exists():
        print(f"Root not found: {root}", file=sys.stderr)
        return 2

    logging.info("Loading configs from %s", root)
    datas = load_all(root)
    if not datas:
        print(f"No Config */ folders with data found under {root}", file=sys.stderr)
        return 1

    stats_by_name: dict[str, "object"] = {}
    pairs = []
    for data in datas:
        logging.info("Analyzing Config %s ...", data.name)
        st = analyze_config(data)
        stats_by_name[data.name] = st
        pairs.append((data, st))

    data_by_name = {d.name: d for d in datas}
    comparison = compare_configs(data_by_name, stats_by_name)

    out_path = args.output if args.output.is_absolute() else (root / args.output)
    render_report(root, pairs, comparison, out_path)
    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
