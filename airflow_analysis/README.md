# airflow_stats

Analyze LibreHardwareMonitor (LHM) CSV logs to evaluate PC airflow configurations.

For each `Config X/` folder under a root directory (with a `setup.md` describing
the fan layout and one or more `LibreHardwareMonitorLog-*.csv` logs), the tool:

- parses the LHM two-row header (sensor path + friendly label) and concatenates
  all CSVs into one tidy per-config DataFrame keyed by sensor path,
- classifies sensors (motherboard/CPU/GPU/storage temps, fan RPM, fan duty,
  loads, power),
- computes summary statistics, Spearman + Pearson correlations, load-binned
  conditional medians, and lagged load->temperature cross-correlations,
- compares configurations pairwise (delta heatmaps at matched load bins, fan
  efficiency slopes, ranked insights),
- emits a single self-contained `report.html` with embedded plots.

## Install

```bash
cd /path/to/Libremonitor
python -m venv .venv && source .venv/bin/activate
pip install -e airflow_analysis
```

## Run

```bash
python -m airflow_stats . -o report.html
open report.html
```

## Annotating `setup.md` (optional but recommended)

The free-form description is kept as-is. To map the generic
`System Fan #1..#6` headers to physical slots, add an optional fenced yaml
block to each `Config X/setup.md`:

````markdown
FRONT 1,2,3, top to bottom
TOP 1,2, front to back

FRONT 2 Noctua
TOP 1,2 Redux pair
CPU 2 noctuas

```yaml
fan_map:
  "System Fan #1": FRONT_1
  "System Fan #2": FRONT_2
  "System Fan #3": FRONT_3
  "System Fan #4": TOP_1
  "System Fan #5": TOP_2
fans:
  FRONT_2: { brand: Noctua }
  TOP_1:   { brand: Arctic_Redux }
  TOP_2:   { brand: Arctic_Redux }
  CPU:     { brand: Noctua, count: 2 }
```
````

When the yaml block is missing, the tool warns and skips per-slot attribution
while still running every other analysis.
