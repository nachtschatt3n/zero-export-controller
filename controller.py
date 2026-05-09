#!/usr/bin/env python3
"""Zero-export controller for Hoymiles micro-inverters via OpenDTU + Tibber Pulse.

Reads grid power and tuning helpers from Home Assistant via REST, computes
per-inverter power limits using a slow-approximation P controller with a
system-wide cap and burst-capable per-inverter ceiling, then writes the
limits back through the HA OpenDTU integration's number entities.
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from prometheus_client import Counter, Gauge, Histogram, start_http_server


HA_BASE_URL = os.environ["HA_BASE_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8080"))
HEARTBEAT_PATH = Path(os.environ.get("HEARTBEAT_PATH", "/tmp/heartbeat"))

GRID_SENSOR = os.environ.get(
    "GRID_SENSOR", "sensor.tibber_pulse_schulstrasse_105_power"
)
PV_TOTAL_SENSOR = os.environ.get(
    "PV_TOTAL_SENSOR", "sensor.opendtu_3c647c_ac_power"
)
INVERTERS = os.environ.get("INVERTERS", "s1,s2,s3").split(",")

MAX_STEP_W = float(os.environ.get("MAX_STEP_W", "200"))
SET_VALUE_DEADBAND_W = float(os.environ.get("SET_VALUE_DEADBAND_W", "5"))
STALE_AFTER_S = float(os.environ.get("STALE_AFTER_S", "30"))


m_grid = Gauge("zec_grid_power_watts", "Latest grid power reading (W); negative = export")
m_pv_total = Gauge("zec_pv_total_watts", "Latest PV total AC power (W)")
m_target = Gauge("zec_target_watts", "Active grid target setpoint (W)")
m_desired = Gauge("zec_desired_total_watts", "Computed desired total inverter limit (W)")
m_limit = Gauge(
    "zec_inverter_limit_watts", "Effective per-inverter limit (W)", ["inverter"]
)
m_ceiling = Gauge(
    "zec_inverter_ceiling_watts",
    "Per-inverter water-fill ceiling for this tick (W); shrinks when sun-limited",
    ["inverter"],
)
m_pv_per = Gauge(
    "zec_inverter_power_watts", "Per-inverter live AC power (W)", ["inverter"]
)
m_iters = Counter("zec_loop_iterations_total", "Loop iterations completed")
m_errs = Counter("zec_loop_errors_total", "Loop errors", ["kind"])
m_enabled = Gauge("zec_enabled", "1 if controller is enabled by kill-switch helper")
m_dry_run = Gauge("zec_dry_run", "1 if controller is in dry-run mode")
m_request = Histogram(
    "zec_ha_request_seconds", "HA REST request duration (s)", ["op"]
)


@dataclass
class HAState:
    state: str
    last_updated: datetime

    @property
    def numeric(self) -> float | None:
        try:
            return float(self.state)
        except (TypeError, ValueError):
            return None

    @property
    def is_on(self) -> bool:
        return self.state == "on"

    @property
    def is_stale(self) -> bool:
        age = (datetime.now(timezone.utc) - self.last_updated).total_seconds()
        return age > STALE_AFTER_S


class HAClient:
    def __init__(self, base_url: str, token: str):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def aclose(self):
        await self._client.aclose()

    async def get_state(self, entity_id: str) -> HAState | None:
        with m_request.labels(op="get_state").time():
            try:
                r = await self._client.get(f"/api/states/{entity_id}")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                data = r.json()
                return HAState(
                    state=data["state"],
                    last_updated=datetime.fromisoformat(data["last_updated"]),
                )
            except httpx.HTTPError as e:
                m_errs.labels(kind="ha_get").inc()
                logging.warning("HA get_state %s failed: %s", entity_id, e)
                return None

    async def get_states(self, entity_ids: list[str]) -> dict[str, HAState | None]:
        results = await asyncio.gather(*(self.get_state(eid) for eid in entity_ids))
        return dict(zip(entity_ids, results))

    async def call_service(self, domain: str, service: str, data: dict) -> bool:
        with m_request.labels(op="call_service").time():
            try:
                r = await self._client.post(
                    f"/api/services/{domain}/{service}", json=data
                )
                r.raise_for_status()
                return True
            except httpx.HTTPError as e:
                m_errs.labels(kind="ha_service").inc()
                logging.warning("HA call_service %s.%s failed: %s", domain, service, e)
                return False


@dataclass
class TuningParams:
    target_w: float
    cap_w: float
    per_max_w: float
    loop_period_s: float
    slow_approx: float
    enabled: bool


@dataclass
class InverterState:
    name: str
    power_w: float
    reachable: bool


SHADE_MARGIN_W = 30.0       # actual must trail current limit by more than this to be "sun-limited"
SHADE_HEADROOM_W = 50.0     # headroom granted above actual production for shaded inverter ceiling


def compute_ceilings(
    inverters: list[InverterState],
    per_max_w: float,
    last_limits: dict[str, float],
) -> dict[str, float]:
    """Per-inverter water-fill ceiling for the next tick.

    East/west panel mismatch is the dominant case: one side is sun-limited,
    the other is limit-bound. A naive equal-share would set both inverters to
    desired/2 and the shaded one would underproduce, draining the productive
    one's allocation across loops. Production-aware ceilings cap the shaded
    inverter near its actual output and free the headroom for the productive
    inverter to consume up to its hardware limit.

    Recovery is automatic: as soon as actual catches up to the (tight) limit,
    the inverter looks limit-bound on the next tick and gets the full
    per_max_w ceiling restored.
    """
    out: dict[str, float] = {}
    for inv in inverters:
        if not inv.reachable:
            out[inv.name] = 0.0
            continue
        prev_limit = last_limits.get(inv.name, per_max_w)
        if inv.power_w + SHADE_MARGIN_W < prev_limit:
            out[inv.name] = max(0.0, min(per_max_w, inv.power_w + SHADE_HEADROOM_W))
        else:
            out[inv.name] = per_max_w
    return out


def distribute(
    desired: float,
    inverters: list[InverterState],
    per_max_w: float,
    ceilings: dict[str, float] | None = None,
) -> dict[str, float]:
    """Water-fill `desired` watts across reachable inverters.

    Each inverter's allocation is capped by `ceilings[name]` if provided,
    otherwise by `per_max_w`. The water-fill loop redistributes any leftover
    capacity from inverters that hit their cap to those that haven't.
    """
    if ceilings is None:
        ceilings = {
            inv.name: per_max_w if inv.reachable else 0.0 for inv in inverters
        }
    limits = {inv.name: 0.0 for inv in inverters}
    pool = [
        inv.name for inv in inverters
        if inv.reachable and ceilings.get(inv.name, 0.0) > 0.0
    ]
    if not pool or desired <= 0:
        return limits

    remaining = desired
    while remaining > 0.5 and pool:
        share = remaining / len(pool)
        next_pool = []
        for name in pool:
            headroom = ceilings[name] - limits[name]
            grant = min(share, max(0.0, headroom))
            limits[name] += grant
            remaining -= grant
            if ceilings[name] - limits[name] > 0.5:
                next_pool.append(name)
        if next_pool == pool:
            break
        pool = next_pool
    return limits


def compute_desired(grid_w: float, pv_total_w: float, params: TuningParams) -> float:
    error = grid_w - params.target_w
    delta = max(-MAX_STEP_W, min(MAX_STEP_W, error * params.slow_approx))
    return max(0.0, min(params.cap_w, pv_total_w + delta))


async def fetch_tuning(ha: HAClient) -> TuningParams | None:
    helpers = {
        "target_w": "input_number.solar_target_grid_power",
        "cap_w": "input_number.solar_max_total_watts",
        "per_max_w": "input_number.solar_per_inverter_max_watts",
        "loop_period_s": "input_number.solar_loop_period_s",
        "slow_approx": "input_number.solar_slow_approx",
        "enabled": "input_boolean.solar_zero_export_enabled",
    }
    states = await ha.get_states(list(helpers.values()))
    try:
        return TuningParams(
            target_w=states[helpers["target_w"]].numeric,
            cap_w=states[helpers["cap_w"]].numeric,
            per_max_w=states[helpers["per_max_w"]].numeric,
            loop_period_s=states[helpers["loop_period_s"]].numeric,
            slow_approx=states[helpers["slow_approx"]].numeric,
            enabled=states[helpers["enabled"]].is_on,
        )
    except (AttributeError, KeyError, TypeError) as e:
        logging.error("Failed to read tuning helpers: %s", e)
        m_errs.labels(kind="tuning").inc()
        return None


async def fetch_inverters(ha: HAClient, names: list[str]) -> list[InverterState]:
    entity_ids = []
    for name in names:
        entity_ids.append(f"sensor.{name}_power")
        entity_ids.append(f"binary_sensor.{name}_reachable")
    states = await ha.get_states(entity_ids)
    out = []
    for name in names:
        power = states.get(f"sensor.{name}_power")
        reach = states.get(f"binary_sensor.{name}_reachable")
        power_w = power.numeric if power and power.numeric is not None else 0.0
        # binary_sensor.*_reachable only updates last_updated on transitions; a
        # stable-on sensor will look "stale" but is in fact authoritative. HA
        # surfaces lost contact as state="unavailable", which .is_on rejects.
        reachable = bool(reach and reach.is_on)
        out.append(InverterState(name=name, power_w=power_w, reachable=reachable))
    return out


async def loop_once(
    ha: HAClient, last_limits: dict[str, float]
) -> tuple[dict[str, float], float]:
    m_iters.inc()
    HEARTBEAT_PATH.write_text(datetime.now(timezone.utc).isoformat())

    tuning = await fetch_tuning(ha)
    if not tuning:
        return last_limits, 20.0
    m_target.set(tuning.target_w)
    m_enabled.set(1 if tuning.enabled else 0)

    grid_state, pv_state = await asyncio.gather(
        ha.get_state(GRID_SENSOR), ha.get_state(PV_TOTAL_SENSOR)
    )
    inverters = await fetch_inverters(ha, INVERTERS)
    for inv in inverters:
        m_pv_per.labels(inverter=inv.name).set(inv.power_w)

    grid_ok = grid_state and grid_state.numeric is not None and not grid_state.is_stale
    pv_ok = pv_state and pv_state.numeric is not None and not pv_state.is_stale
    if grid_state and grid_state.numeric is not None:
        m_grid.set(grid_state.numeric)
    if pv_state and pv_state.numeric is not None:
        m_pv_total.set(pv_state.numeric)

    if not tuning.enabled:
        # Kill switch off: hand control back to the user. Do NOT write any
        # limits this tick — last RAM-resident limit stays. Note: the legal
        # 800 W cap is then only as good as the inverter's persistent_limit
        # config (set out-of-band in OpenDTU).
        logging.info("kill switch off: skipping writes; last limits remain in effect")
        return last_limits, tuning.loop_period_s
    ceilings = compute_ceilings(inverters, tuning.per_max_w, last_limits)
    for name, c in ceilings.items():
        m_ceiling.labels(inverter=name).set(c)

    if not grid_ok or not pv_ok:
        # Sensors stale or missing: fail to a safe state that respects the
        # legal 800 W cap regardless of how many inverters are reachable.
        logging.warning(
            "safe fallback: grid_ok=%s pv_ok=%s — distributing cap_w across reachable",
            grid_ok, pv_ok,
        )
        new_limits = distribute(tuning.cap_w, inverters, tuning.per_max_w, ceilings)
        m_desired.set(tuning.cap_w)
    else:
        desired = compute_desired(grid_state.numeric, pv_state.numeric, tuning)
        m_desired.set(desired)

        new_limits = distribute(desired, inverters, tuning.per_max_w, ceilings)
        logging.info(
            "grid=%.0fW pv=%.0fW target=%.0fW desired=%.0fW ceilings=%s limits=%s",
            grid_state.numeric, pv_state.numeric, tuning.target_w, desired,
            {k: round(v) for k, v in ceilings.items()},
            {k: round(v) for k, v in new_limits.items()},
        )

    for name, limit in new_limits.items():
        m_limit.labels(inverter=name).set(limit)
        prev = last_limits.get(name, -1.0)
        if abs(limit - prev) < SET_VALUE_DEADBAND_W:
            continue
        if DRY_RUN:
            logging.info(
                "[dry-run] would set number.%s_limit_nonpersistent_absolute=%.0f",
                name, limit,
            )
        else:
            await ha.call_service(
                "number",
                "set_value",
                {
                    "entity_id": f"number.{name}_limit_nonpersistent_absolute",
                    "value": round(limit),
                },
            )

    return new_limits, tuning.loop_period_s


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("zero-export-controller starting (dry_run=%s)", DRY_RUN)
    m_dry_run.set(1 if DRY_RUN else 0)
    start_http_server(METRICS_PORT)

    ha = HAClient(HA_BASE_URL, HA_TOKEN)
    last_limits: dict[str, float] = {}
    try:
        while True:
            try:
                last_limits, sleep_s = await loop_once(ha, last_limits)
            except Exception:
                logging.exception("loop iteration failed")
                m_errs.labels(kind="loop").inc()
                sleep_s = 20.0
            await asyncio.sleep(max(5.0, sleep_s))
    finally:
        await ha.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
