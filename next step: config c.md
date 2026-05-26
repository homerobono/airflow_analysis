Polish applied. The matrix now reads `FRONT_3 (Pump Fan #1)`, `TOP_1 (System Fan #1)`, etc., so you can see physical slot at a glance. (`TOP_1_TOP_2` shows up in Config A because both Reduxes share one connector.) Open `report.html` and the **Best fan per temperature target** and **Full fan-impact table** are now slot-labeled.

---

Now the curve plan. I'll work from the Fan Impact Matrix you already have (Config B), the airflow physics you described, and what the MSI MAG X870 Tomahawk Wi-Fi actually exposes.

## What the MSI X870 Tomahawk BIOS gives you per fan

Click BIOS X for AM5/X870 boards lets you, per fan header:

- Mode: **PWM** or **DC** (so a 3-pin Redux works fine on any case-fan header that lets you flip the mode — `PUMP_FAN1`, `SYS_FAN1..4`, `CPU_FAN1`)
- Source: **CPU**, **System**, **MOS** (VRM), **Chipset**
- Smart Fan curve: typically 4 anchor points (T, duty %)
- **Step Up Time** and **Step Down Time** (seconds per duty step) — *this is the knob for "smooth, no hunting"*
- Min duty (so the fan doesn't drop below stall)

Critically: the BIOS curve cannot use GPU temperature. If you want a GPU-aware curve on a case fan, you have to do it from the OS with **FanControl** (which reads LHM sensors). I'll cover both paths.

## Physics in your case (Config B)

You correctly described it: the chassis is open, the GPU is horizontal mid-bottom and aligned vertically with the (empty) TOP_2 slot, the CPU tower fans point at the rear and act as a partial rear exhaust. So the airflow vectors are:

- **FRONT_3 (Noctua S12A, top of front stack):** intake aimed at CPU heatsink / VRM area.
- **FRONT_2 (Redux, mid front):** intake that mostly bathes the GPU compartment / chipset / RAM.
- **TOP_1 (Redux, back-top):** exhaust pulling from the CPU/VRM area (above the socket).
- **TOP_2 (empty, front-top):** the natural exit point for GPU heat — and that's the hole that isn't being actively serviced. That's exactly the asymmetry the data shows.
- **CPU heatsink fans + GPU axials** are mostly internal recirculation/aimed exhaust assist.

So your two case-fan jobs split cleanly:

- **CPU/VRM/exhaust path** = FRONT_3 (intake into the path) + TOP_1 (exhaust out of the path) → both should respond to **CPU** temperature (and ideally MOS as a backstop).
- **GPU/case-bulk path** = FRONT_2 (bulk intake under the GPU) + (no dedicated exhaust) → wants to respond to a slow-moving "case heat" signal, since you can't drive it from GPU temp in BIOS.

## Target balance

Approximate CFM at max RPM for your fans:

- NF-P14s redux-1200 @ 1200 RPM ≈ 64 CFM
- NF-S12A PWM @ 1200 RPM ≈ 63 CFM
- 2× NF-A9 PWM on the CPU heatsink (one front-fixed + one rear extra) ≈ ~35–45 CFM combined of rear-directed flow at moderate RPM

For "intake ≈ exhaust" the equation is:

```
FRONT_3 + FRONT_2  ≈  TOP_1 + (CPU rear-exhaust contribution)
```

Since the two intakes sum to ~127 CFM at max and TOP_1 alone caps at ~64 CFM, the CPU rear fans have to make up the rest. Under heavy load with CPU fans at ~70%, they contribute ~25–30 CFM, so 64 + 28 ≈ 92 CFM exhaust vs ~127 CFM intake at max — you'll always be **slightly positive pressure** at the top of the curve. Given your open chassis, that's fine; positive pressure also helps the GPU regression because air leaks out through the empty TOP_2 hole instead of warm air pooling there.

So **don't try to match exhaust to intake at the top of the curve** — accept slight positive pressure and instead ensure exhaust **scales linearly with intake** so the *ratio* stays stable.

## Recommended curves

Goal: smooth, slow-moving, hysteretic, with the two CPU-path fans (FRONT_3 + TOP_1) moving in sync, and FRONT_2 plodding along on the slow System sensor so it doesn't whine.

### FRONT_3 — Noctua S12A PWM (intake, CPU-targeted)
- **Header**: PUMP_FAN1 (or any SYS_FAN), set mode to **PWM**.
- **Source**: **CPU**.
- **Curve** (4-point, T → duty %):

| CPU °C | PWM % | approx RPM |
|---|---|---|
| ≤ 40 | 35 | ~420 |
| 55 | 50 | ~600 |
| 70 | 75 | ~900 |
| ≥ 85 | 100 | 1200 |

- **Step Up Time**: ~1.5 s/step (so it tracks CPU spikes reasonably).
- **Step Down Time**: ~4 s/step (so it doesn't yo-yo when CPU drops back to idle between bursts).

### TOP_1 — NF-P14s redux-1200 DC (sole exhaust, CPU/VRM path)
- **Header**: SYS_FAN1, set mode to **DC**.
- **Source**: **CPU** (so it tracks FRONT_3). If your BIOS lets you blend, use a 70% CPU / 30% MOS source; otherwise pure CPU is the right call — the matrix confirmed this fan does VRM cooling indirectly via the CPU path.
- **Curve**:

| CPU °C | DC % | approx RPM |
|---|---|---|
| ≤ 40 | 40 | ~480 |
| 55 | 60 | ~720 |
| 70 | 85 | ~1020 |
| ≥ 85 | 100 | ~1200 |

- **Step Up**: 1.5 s/step. **Step Down**: 4 s/step. (Same as FRONT_3 — they need to move together.)
- **Minimum start duty**: 35% (Reduxes can stall below ~30% on DC).

Notice TOP_1 sits a tick higher than FRONT_3 at every temperature: this is intentional, so that when the CPU heats up the exhaust rises *just before* the intake catches up. Slight positive pressure delta in the CPU path is what gives you good evacuation.

### FRONT_2 — NF-P14s redux-1200 DC (bulk intake, GPU/case path)
This is the one without a good BIOS-side sensor (GPU temp is invisible to BIOS).

- **Header**: SYS_FAN2, set mode to **DC**.
- **Source**: **System** (NCT6687 motherboard sensor). It tracks "ambient heat inside the case", which is what GPU dumps into.
- **Curve**:

| System °C | DC % | approx RPM |
|---|---|---|
| ≤ 35 | 40 | ~480 |
| 45 | 55 | ~660 |
| 55 | 75 | ~900 |
| ≥ 65 | 100 | ~1200 |

- **Step Up**: 4 s/step. **Step Down**: 6 s/step. (Slow on purpose — System sensor moves slowly, and the GPU heat plume builds over tens of seconds, not milliseconds.)
- **Minimum start duty**: 35%.

Why System and not MOS for this fan? Your matrix showed that FRONT_2 in B was sitting at 47% / 580 RPM because its source was the System sensor and System barely moved. The fan never had to respond. With the curve above and the System sensor sitting around 50–60 °C under load, it will float around 60–80%, which is exactly where you want it.

### CPU_HEATSINK — 2× NF-A9 PWM (rear-pointing exhaust assist)
- **Header**: CPU_FAN1, **PWM**.
- **Source**: **CPU**.
- **Curve**:

| CPU °C | PWM % |
|---|---|
| ≤ 40 | 30 |
| 55 | 45 |
| 70 | 70 |
| ≥ 85 | 100 |

- **Step Up**: 1.0 s/step. **Step Down**: 5 s/step.

## Anti-hunt checklist

The reason fans "go up and down too frequently" is almost always one of:

1. **Step Up = Step Down** (symmetric ramps oscillate). Always set **Step Down ~2–3× longer** than Step Up.
2. **Steep slopes** between two close points. The shallowest curve that still gets you to 100% at 85 °C is the smoothest.
3. **Using CPU Tctl on a DC fan**. Tctl is *spiky*; AMD's per-CCD die temp wiggles a few degrees several times per second. Pair Tctl curves with longer Step Up times (≥1.5 s) and asymmetric Step Down (≥4 s), or use a smoother source (System, MOS).
4. **Min duty too low**. Below ~25–30% the Redux DC fans can stall and the controller will hunt to find a duty that keeps them spinning. Pin minimum at 35%.

## If you want the GPU-aware version (recommended for FRONT_2)

Install **FanControl** (open-source, free; reads from LibreHardwareMonitor under the hood, so it'll see all the same sensors LHM logs). It lets you:

- Use **GPU Hot Spot** (or a mix curve: max of GPU hotspot and System) as the source for FRONT_2.
- Define a "Mix" sensor like `max(GPU_hotspot - 20, System)` so the fan responds to whichever is hotter relative to its scale.
- Set per-fan time constants (one slider, in seconds).

The settings I'd start with there for FRONT_2:

- Source: `max(GPU Hot Spot − 25 °C, System)` (subtracting 25 puts the GPU on the System sensor's scale)
- Curve: `45 °C → 40 %`, `55 °C → 60 %`, `65 °C → 85 %`, `75 °C → 100 %`
- Response time: **20 s** (FanControl uses a single low-pass smoothing time)

That single change will likely close the GPU hotspot gap between A and B entirely while keeping the noise profile of the smoother curve.

## Verification loop

1. Apply curves, run 2–3 days of mixed workload, regenerate `report.html`.
2. Look at the new **Fan Impact Matrix** rows for FRONT_2, FRONT_3, TOP_1 — with deliberately *more* RPM variance from these curves, the tolerance values will rise and the coefficients become trustworthy.
3. Compare CPU/VRM/GPU rise-over-ambient to the current Config B numbers. Targets:
   - CPU rise ≤ 12 °C (matched or better than current B)
   - VRM rise ≤ 1 °C
   - GPU hotspot rise ≤ 10.5 °C (closing the gap to A)
   - Total fan RPM @ load 5000–6000 RPM (similar to B, lower wouldn't be honest given the work the case is doing)
4. If GPU still lags, the cleanest next physical change is to **add a third Redux as TOP_2** — but the curve tuning is the cheap experiment to run first.

If you do all this and want to log the results as `Config C` for a 3-way comparison, just create `Config C/setup.md` with a `fan_map:` block and drop logs in — the tool will pick it up automatically.
