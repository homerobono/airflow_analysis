# FanControl profile

Optimized FanControl 268 NET .10 (Windows 11) profile generated from the airflow
analysis in `report.html` and the LibreHardwareMonitor CSV logs across Config A,
B, C, and D. Goal: best cooling efficiency with a 60/40 CPU-to-GPU weighting,
silent idle (case fans allowed to stall), tolerated noise at high load,
hotspot/CPU safety overrides baked into the curves.

The Config D capture scored poorly because the V5 score saw both a high GPU
hotspot rise over ambient and the highest total fan RPM. Its top-30% GPU samples
had roughly 23 C GPU-hotspot rise while the high-load fan sum was about 6968 RPM.
For comparability with Configs A-C, the GPU fan motors are left on NVIDIA's
stock curve; this profile only adjusts the CPU and chassis fans.

## Files

- `userConfig.json` - new optimized profile to import.
- `userConfig.previous.json` - snapshot of the prior profile before this change.

## What's inside the profile

- **CPU Fan**: graph curve on `Core (Tctl/Tdie)` with steps from 45->22% up to
  84->100%; the 58-82 C band is slightly stronger than the first Config D
  profile to claw back CPU rise without relying as much on chassis RPM.
- **Pump Fan / FRONT_2 intake**, **System Fan #1 / FRONT_3 intake**, **System
  Fan #2 / TOP_1 exhaust**: each is a `Mix(Max)` of a CPU-anchored graph and a
  GPU-anchored graph. The CPU-driven midrange is trimmed versus the first Config
  D profile, while the GPU-driven midrange starts earlier. Because FanControl's
  mixer only supports
  Min/Max/Avg/Sum (no arbitrary numeric weighting), the CPU graph is scaled
  higher than the GPU graph in the operating band so the resulting Max tracks
  CPU dominantly (~60% influence) while still ramping up if the GPU runs hot
  while the CPU is cool.
- **GPU Fan 1 + 2**: disabled in FanControl, with no selected curve, so the
  NVIDIA stock fan curve controls the card like the other configs. The GPU RPM
  sensors remain visible for logging.
- All other channels (Chipset, System Fan #3-#6, EZ-Connect) stay disabled.

### Dynamics

- `OneWayHysteresis: true` everywhere (fans only step down after temps cross
  the lower threshold).
- Hysteresis: 2 C on the CPU curve and 3 C on chassis curves.
- Response time: 2 (fast) for CPU, 3 (slower) for chassis.
- Step up/down: faster step-up, slower step-down so the chassis fans glide
  back to silent.

### Idle interpretation used to design the floor

From 144,012 CSV samples: idle = CPU load <12% and GPU 3D load <8%, with CPU
temp 53-58 C median and GPU 38-49 C. Below ~45 C CPU and ~52 C GPU, every
chassis fan and the GPU fans sit at 0% (allowed to stall) for silent idle.

### Safety guardrails (baked into curves)

- CPU 82 C -> 95%, 84 C -> 100%.
- Chassis fans at 79-81 C -> 95-100%.
- GPU fans are not controlled by this profile; NVIDIA stock firmware handles
  their safety behavior.

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
  for the chassis fans). GPU Fan 1 and GPU Fan 2 should be disabled/unassigned
  in FanControl so NVIDIA's stock curve owns them.
- Stress-test ~10-15 min of mixed CPU+GPU load and verify:
  - CPU sustained <=75 C
  - GPU Core <=72 C, GPU Hot Spot <=86 C
  - Idle acoustics near-silent, some case fans showing 0%
- If CPU runs hot first: raise CPU fan points at 64-75 C by +5% in the UI.
- If GPU hotspot still runs hot first: raise the GPU-driven chassis curves
  before changing NVIDIA's stock GPU fan behavior.

## Rollback

`userConfig.previous.json` is the prior profile. To revert, follow Option A or
B with that file instead.
