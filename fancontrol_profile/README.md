# FanControl profile

Optimized FanControl 268 NET .10 (Windows 11) profile generated from the airflow
analysis in `report.html` and the LibreHardwareMonitor CSV logs across Config A,
B, and C. Goal: best cooling efficiency with a 60/40 CPU-to-GPU weighting,
silent idle (case fans allowed to stall), tolerated noise at high load,
hotspot/CPU safety overrides baked into the curves.

## Files

- `userConfig.json` - new optimized profile to import.
- `userConfig.previous.json` - snapshot of the prior profile before this change.

## What's inside the profile

- **CPU Fan**: graph curve on `Core (Tctl/Tdie)` with steps from 45->22% up to
  84->100%; safety steps at 82->95% and 84->100%.
- **Pump Fan / FRONT_2 intake**, **System Fan #1 / FRONT_3 intake**, **System
  Fan #2 / TOP_1 exhaust**: each is a `Mix(Max)` of a CPU-anchored graph and a
  GPU-anchored graph. Because FanControl's mixer only supports
  Min/Max/Avg/Sum (no arbitrary numeric weighting), the CPU graph is scaled
  higher than the GPU graph in the operating band so the resulting Max tracks
  CPU dominantly (~60% influence) while still ramping up if the GPU runs hot
  while the CPU is cool.
- **GPU Fan 1 + 2**: `Mix(Max)` of a GPU Core graph and a GPU Hot Spot safety
  graph that forces 90% at 85 C and 100% at 90 C.
- All other channels (Chipset, System Fan #3-#6, EZ-Connect) stay disabled.

### Dynamics

- `OneWayHysteresis: true` everywhere (fans only step down after temps cross
  the lower threshold).
- Hysteresis: 2 C on CPU/GPU-core curves, 3 C on chassis curves, 2 C on the
  GPU hotspot safety curve.
- Response time: 1-2 (fast) for CPU/GPU/safety, 3 (slower) for chassis.
- Step up/down: faster step-up, slower step-down so the chassis fans glide
  back to silent.

### Idle interpretation used to design the floor

From 144,012 CSV samples: idle = CPU load <12% and GPU 3D load <8%, with CPU
temp 53-58 C median and GPU 38-49 C. Below ~45 C CPU and ~52 C GPU, every
chassis fan and the GPU fans sit at 0% (allowed to stall) for silent idle.

### Safety guardrails (baked into curves)

- CPU 82 C -> 95%, 84 C -> 100%.
- Chassis fans at 79-81 C -> 95-100%.
- GPU Core 86 C -> 100%; GPU Hot Spot 85 C -> 90%, 90 C -> 100%.

## How to import in FanControl 268 NET .10 (Windows 11)

The application reads its profile from
`%LOCALAPPDATA%\FanControl\Configurations\userConfig.json` (this is also where
the existing file you shared lives:
`C:\Users\<you>\Downloads\Fan Control Configurations\` is just a copy/backup
folder, not the live one). There are two equivalent ways to load this profile:

### Option A: load through the FanControl UI (recommended)

1. Copy `userConfig.json` from this folder onto the Windows machine, into a
   path FanControl can see (any folder works, e.g.
   `C:\Users\<you>\Documents\FanControl\`).
2. Open FanControl.
3. Click the gear/settings icon (top-right) -> `Load configuration`.
4. Select the copied `userConfig.json`. FanControl will replace the active
   config and immediately apply the new curves.
5. (Optional) Click `Save configuration` to write it as the default
   `userConfig.json` in `%LOCALAPPDATA%\FanControl\Configurations\`.

### Option B: replace the live config file directly

1. Quit FanControl (right-click the tray icon -> Exit).
2. Open `%LOCALAPPDATA%\FanControl\Configurations\` in Explorer.
3. Rename the existing `userConfig.json` to `userConfig.backup.json`.
4. Copy this repo's `userConfig.json` into that folder.
5. Start FanControl. It will load the new profile on launch.

### After import

- Confirm in the UI that each active control card shows
  `ManualControl = false` and the expected curve (CPU graph for CPU Fan, Mix
  for the chassis fans, Mix with hotspot safety for the GPU fans).
- Stress-test ~10-15 min of mixed CPU+GPU load and verify:
  - CPU sustained <=75 C
  - GPU Core <=72 C, GPU Hot Spot <=86 C
  - Idle acoustics near-silent, some case fans showing 0%
- If CPU runs hot first: raise CPU fan points at 64-75 C by +5% in the UI.
- If GPU runs hot first: raise GPU fan points at 64-82 C by +6% and System
  Fan #2 by +4% in the 63-75 C band.

## Rollback

`userConfig.previous.json` is the prior profile. To revert, follow Option A or
B with that file instead.
