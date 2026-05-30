FRONT 1,2,3, bottom to top
TOP 1,2, back to front

FRONT 2 Noctua NF-S12A PWM
TOP 1,2 Noctua NF-P14s redux-1200 pair on the same connector
CPU heatsink has 2 fans: fixed front fan plus extra Noctua NF-A9 PWM on the back

```yaml
fan_map:
  "CPU Fan": CPU_HEATSINK
  "Pump Fan": FRONT_2
  "Pump Fan #1": FRONT_2
  "System Fan #1": TOP_1_TOP_2
fans:
  FRONT_2:
    model: Noctua NF-S12A
    position: "front #2"
    control: PWM
    connector: Pump Fan
  TOP_1_TOP_2:
    model: Noctua NF-P14s redux-1200
    count: 2
    positions: ["top #1", "top #2"]
    control: DC
    connector: System Fan #1
    note: both fans share the same motherboard connector
  CPU_HEATSINK:
    count: 2
    position: CPU heatsink
    connector: CPU Fan
    note: front heatsink fan stays installed; rear fan is the extra Noctua NF-A9 PWM
```
