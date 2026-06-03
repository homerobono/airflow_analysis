FRONT 1,2,3, bottom to top
TOP 1,2, back to front

FRONT 2 Noctua NF-P14s redux-1200
FRONT 3 Noctua NF-S12A PWM
TOP 1 Noctua NF-P14s redux-1200
CPU heatsink has 2 fans: fixed front fan plus extra Noctua NF-A9 PWM on the back

This is the same Config C physical setup, but fan control is driven by the
FanControl 268 NET .10 profile in `../fancontrol_profile/userConfig.json`
(Windows 11) instead of BIOS-only fan curves. See
`../fancontrol_profile/README.md` for the profile design and import steps.

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
    note: physically verified for Configs B and C (Pump header drives the front-middle Redux)
  FRONT_3:
    model: Noctua NF-S12A
    position: "front #3"
    control: PWM
    connector: System Fan #1
    confidence: high
    note: physically verified for Configs B and C (Sys Fan #1 drives the front-top Noctua S12A)
  TOP_1:
    model: Noctua NF-P14s redux-1200
    position: "top #1"
    control: DC
    connector: System Fan #2
    confidence: high
    note: physically verified for Configs B and C (Sys Fan #2 drives the rear-top Redux exhaust)
  CPU_HEATSINK:
    count: 2
    position: CPU heatsink
    connector: CPU Fan
    note: front heatsink fan stays installed; rear fan is the extra Noctua NF-A9 PWM
fan_control:
  source: FanControl 268 NET .10 (Windows 11)
  profile: ../fancontrol_profile/userConfig.json
  notes: >
    Software fan control via FanControl replaces the BIOS curve used in
    Config C. CPU/GPU-aware curves with hotspot safety overrides; see
    ../fancontrol_profile/README.md.
```
