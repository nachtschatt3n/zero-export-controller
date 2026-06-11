# zero-export-controller

A small zero-feed-in (Nulleinspeisung) controller for Hoymiles micro-inverters
behind [OpenDTU](https://github.com/tbnobody/OpenDTU), driven by Home
Assistant. Reads grid power from any HA sensor (e.g. Tibber Pulse), computes
new per-inverter power limits, and writes them back through the HA OpenDTU
integration's `number.*_limit_nonpersistent_absolute` entities.

## Why this exists

A balcony solar setup (Balkonkraftwerk) without dynamic limiting feeds the
grid for free whenever the panels produce more than the household consumes.
At the same time, the German Bagatellgrenze caps total feed-in at 800 W; an
unconstrained system can violate the legal limit on bright days.

Existing solutions (`reserve85/HoymilesZeroExport`, HA blueprints) work, but
neither handled two requirements specific to this setup:

1. **800 W system cap with single-inverter burst** — sum ≤ 800 W, but any one
   inverter can rise to its 600 W hardware ceiling when others are offline or
   shaded.
2. **East/west panel mismatch is the normal case** — naive equal-share
   distribution leaves harvest on the table when one inverter is sun-limited.

This controller is purpose-built around those two points.

## How it works

```
Tibber Pulse / smart meter → HA (sensor.*_power)
                                ↓ REST + long-lived token
                       zero-export-controller
                                ↓ HA service: number.set_value
              number.s{1,2,3}_limit_nonpersistent_absolute
                                ↓ HACS OpenDTU integration → MQTT → radio
                              S1 / S2 / S3 inverters
```

Every loop tick (default 30 s; Hoymiles needs ~18 s to respond, so periods
below 20 s are unsupported):

1. Read grid power, PV total, per-inverter power & reachability, plus the
   six tunable HA helpers.
2. **Feed-forward consumption tracker** computes the desired total output:
   `consumption = ema(grid + pv_total)`, then
   `desired = clamp(consumption − target, 0, cap_w)`.
   Household consumption is independent of where the limits currently sit,
   so desired lands on the right answer in one tick — when consumption
   exceeds the cap (most of the day), desired pins at `cap_w` and the
   inverters run at maximum with zero writes. `slow_approx` is the EMA
   smoothing factor (1.0 = no smoothing; ~0.3 = a few ticks of memory),
   which rejects meter noise without slowing structural shifts. A negative
   `target` tolerates that much sustained feed-in.
3. **`compute_ceilings()`** decides each inverter's water-fill ceiling for
   this tick:
   - if actual production trails the previous limit by more than `SHADE_MARGIN_W`
     (30 W), the inverter is **sun-limited** → ceiling tightens to
     `actual + SHADE_HEADROOM_W` (50 W).
   - otherwise the ceiling is the full hardware `per_max_w` (600 W).
   Recovery is automatic: as soon as `actual ≈ limit`, the inverter looks
   limit-bound on the next tick and the ceiling is restored to `per_max_w`.
4. **`distribute()`** water-fills the desired total across reachable
   inverters, respecting each ceiling. Headroom freed by sun-limited
   inverters flows to productive ones, up to their 600 W cap.
5. The new limit is pushed via `number.set_value` only when it differs from
   the last *written* value by more than the deadband. The deadband widens
   from `SET_VALUE_DEADBAND_W` (5 W) to `SATURATION_DEADBAND_W` (50 W) when
   grid import exceeds `SATURATION_GRID_THRESHOLD_W` (400 W) — in that
   regime the inverters are running flat-out and W-level precision has no
   benefit, so we suppress the noise-driven write churn.

### Failure modes

- **Kill switch off** (`input_boolean.solar_zero_export_enabled` = off):
  controller skips writes entirely; last RAM-resident limit stays. Use this
  when you need manual control.
- **Sensors stale** (grid or PV reading hasn't updated within
  `STALE_AFTER_S`): controller falls back to writing
  `distribute(cap_w, …)`, which guarantees `sum ≤ 800 W` regardless of
  reachability. A `safe fallback` warning is emitted.
- **Reachability**: `binary_sensor.*_reachable` only updates `last_updated`
  on transitions, so its age is not a staleness signal — the state alone is
  authoritative. Lost contact surfaces as `state="unavailable"`, which is
  treated as not reachable.

## Configuration

All runtime tuning is via HA helpers; only static identity goes in env vars:

| Env var | Default | Notes |
|---|---|---|
| `HA_BASE_URL` | _required_ | e.g. `http://home-assistant:8123` |
| `HA_TOKEN` | _required_ | HA long-lived access token |
| `DRY_RUN` | `true` | When `true`, logs but does not call `number.set_value` |
| `GRID_SENSOR` | `sensor.tibber_pulse_schulstrasse_105_power` | Live grid W; negative = export |
| `PV_TOTAL_SENSOR` | `sensor.opendtu_3c647c_ac_power` | OpenDTU summed AC W |
| `INVERTERS` | `s1,s2,s3` | Inverter name prefixes for `sensor.{name}_power` etc. |
| `METRICS_PORT` | `8080` | Prometheus exposition |
| `STALE_AFTER_S` | `30` | Power-sensor reading age threshold |
| `SET_VALUE_DEADBAND_W` | `25` | Skip `number.set_value` if change is below this (W) |
| `SATURATION_GRID_THRESHOLD_W` | `400` | Above this grid import, widen the deadband |
| `SATURATION_DEADBAND_W` | `50` | Wider deadband used when grid is saturated import |
| `LOG_LEVEL` | `INFO` | Standard Python log level |

Live HA helpers (created by the operator):

| Entity | Type | Default | Range | Purpose |
|---|---|---|---|---|
| `input_number.solar_target_grid_power` | W | 0 | −500…0 | Grid setpoint; negative tolerates that much feed-in |
| `input_number.solar_max_total_watts` | W | 800 | 0…900 | System cap |
| `input_number.solar_per_inverter_max_watts` | W | 600 | 0…650 | Hardware ceiling |
| `input_number.solar_loop_period_s` | s | 30 | 20…120 | Tick period (≥20 s; Hoymiles reaction time) |
| `input_number.solar_slow_approx` | — | 0.30 | 0.05…1.0 | Consumption EMA smoothing factor |
| `input_boolean.solar_zero_export_enabled` | bool | on | — | Kill switch |

## Metrics (`/metrics`)

| Metric | Type | Notes |
|---|---|---|
| `zec_grid_power_watts` | gauge | live grid reading |
| `zec_pv_total_watts` | gauge | OpenDTU summed AC |
| `zec_target_watts` | gauge | active target setpoint |
| `zec_desired_total_watts` | gauge | computed desired total |
| `zec_consumption_est_watts` | gauge | EMA-smoothed household consumption estimate |
| `zec_inverter_limit_watts{inverter}` | gauge | effective per-inverter limit |
| `zec_inverter_ceiling_watts{inverter}` | gauge | water-fill ceiling for this tick |
| `zec_inverter_power_watts{inverter}` | gauge | live per-inverter AC |
| `zec_loop_iterations_total` | counter | tick count |
| `zec_loop_errors_total{kind}` | counter | by error class |
| `zec_writes_skipped_total{reason}` | counter | limit writes suppressed (e.g. by deadband) |
| `zec_effective_deadband_watts` | gauge | active deadband for the current tick |
| `zec_enabled` | gauge | 1 if kill switch is on |
| `zec_dry_run` | gauge | 1 if `DRY_RUN=true` |
| `zec_ha_request_seconds{op}` | histogram | HA REST timing |

Suggested alerts:

- **`ZECOverLegalCap`** (critical) — `sum(zec_inverter_power_watts) > 850` for >30 s.
- **`ZECExportSustained`** (warning) — `zec_grid_power_watts < -100` for >2 m.
- **`ZECPodDown`** / **`ZECLoopStalled`** — pod/loop liveness.

## Deploying on Kubernetes (Flux/Kustomize sketch)

```yaml
# helmrelease.yaml — bjw-s app-template
spec:
  values:
    controllers:
      zero-export-controller:
        containers:
          app:
            image:
              repository: ghcr.io/nachtschatt3n/zero-export-controller
              tag: 0.1.0
            env:
              HA_BASE_URL: http://home-assistant.home-automation.svc.cluster.local:8123
              DRY_RUN: "false"
              METRICS_PORT: "8080"
              GRID_SENSOR: sensor.tibber_pulse_schulstrasse_105_power
              PV_TOTAL_SENSOR: sensor.opendtu_3c647c_ac_power
              INVERTERS: s1,s2,s3
            envFrom:
              - secretRef:
                  name: zero-export-controller-secret  # provides HA_TOKEN
            probes:
              readiness: { http: { path: /metrics, port: 8080 } }
              liveness:  { http: { path: /metrics, port: 8080 } }
    service:
      app:
        controller: zero-export-controller
        ports:
          metrics: { port: 8080 }
```

Run in `DRY_RUN=true` until you've watched at least one full daylight cycle of
clean log lines, then flip to `false`. Default the kill-switch helper to
`off` at first cutover so the live container starts in skip-writes mode; flip
it to `on` from the HA dashboard for a controlled go-live.

## Run locally

```sh
pip install -e '.[dev]'
pytest -v
HA_BASE_URL=http://localhost:8123 HA_TOKEN=... DRY_RUN=true python -u controller.py
```

## Container

Multi-arch (`linux/amd64`, `linux/arm64`) images on every push to `main` and
on every `v*` tag at `ghcr.io/nachtschatt3n/zero-export-controller`. Pin to a
semver tag (`0.1.0`) in production; Renovate or the equivalent will surface
new tags as PRs.

## License

MIT
