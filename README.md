# zero-export-controller

A small zero-feed-in (Nulleinspeisung) controller for Hoymiles micro-inverters
behind [OpenDTU](https://github.com/tbnobody/OpenDTU), driven by Home
Assistant. Reads grid power from any HA sensor (e.g. Tibber Pulse), computes
new per-inverter power limits, and writes them back through the HA OpenDTU
integration's `number.*_limit_nonpersistent_absolute` entities.

## What it does

- Holds the grid at a configurable target (default −50 W, slight import).
- Enforces a system-wide cap (default 800 W — the German Bagatellgrenze) while
  letting any single inverter burst toward its hardware ceiling.
- **Production-aware redistribution**: when one inverter is sun-limited
  (east/west panel mismatch, partial shading) it gives up its allocation to
  productive inverters automatically. Recovery is automatic when the shaded
  inverter catches up to its tightened limit.
- Live tunability via Home Assistant `input_number` / `input_boolean` helpers.
  No restart needed to change setpoints.
- Slow-approximation P-controller; never writes the persistent inverter
  limit (no flash wear).

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

Live HA helpers (created by the operator):

- `input_number.solar_target_grid_power` (W, default −50)
- `input_number.solar_max_total_watts` (W, default 800)
- `input_number.solar_per_inverter_max_watts` (W, default 600)
- `input_number.solar_loop_period_s` (s, default 20)
- `input_number.solar_slow_approx` (default 0.20)
- `input_boolean.solar_zero_export_enabled` (kill switch)

When the kill switch is off, the controller skips writes — last RAM-resident
limits remain. Sensor-stale path falls back to a safe distribution of the
legal cap across reachable inverters.

## Metrics

Exposed at `:8080/metrics`:

- `zec_grid_power_watts`, `zec_pv_total_watts`, `zec_target_watts`
- `zec_desired_total_watts`
- `zec_inverter_limit_watts{inverter}`, `zec_inverter_ceiling_watts{inverter}`,
  `zec_inverter_power_watts{inverter}`
- `zec_loop_iterations_total`, `zec_loop_errors_total{kind}`
- `zec_enabled`, `zec_dry_run`
- `zec_ha_request_seconds` (histogram, by op)

## Run locally

```sh
pip install -e '.[dev]'
pytest -v
HA_BASE_URL=http://localhost:8123 HA_TOKEN=... DRY_RUN=true python -u controller.py
```

## Container

Multi-arch images on every push to `main` and on every `v*` tag at
`ghcr.io/nachtschatt3n/zero-export-controller`.

## License

MIT
