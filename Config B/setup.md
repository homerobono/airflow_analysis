FRONT 1,2,3, bottom to top
TOP 1,2, back to front

FRONT 2 Noctua NF-P14s redux-1200
FRONT 3 Noctua NF-S12A PWM
TOP 1 Noctua NF-P14s redux-1200
CPU heatsink has 2 fans: fixed front fan plus extra Noctua NF-A9 PWM on the back

```yaml
fan_map:
  "CPU Fan": CPU_HEATSINK
  "Pump Fan": FRONT_2
  "Pump Fan #1": FRONT_2
  "System Fan #1": FRONT_3
  "System Fan #2": TOP_1
fans:
  FRONT_2:
    model: Noctua NF-P14s redux-1200
    position: "front #2"
    control: DC
    connector: Pump Fan
    confidence: high
    note: physically verified; Pump Fan header default keeps it near 47% / ~617 RPM
  FRONT_3:
    model: Noctua NF-S12A
    position: "front #3"
    control: PWM
    connector: System Fan #1
    confidence: high
    note: physically verified; this is the only chassis fan with a varying curve in Config B (about 679-1319 RPM)
  TOP_1:
    model: Noctua NF-P14s redux-1200
    position: "top #1"
    control: DC
    connector: System Fan #2
    confidence: high
    note: physically verified; currently pinned near 47% / ~581 RPM by the Sys Fan #2 default
  CPU_HEATSINK:
    count: 2
    position: CPU heatsink
    connector: CPU Fan
    note: front heatsink fan stays installed; rear fan is the extra Noctua NF-A9 PWM
```
