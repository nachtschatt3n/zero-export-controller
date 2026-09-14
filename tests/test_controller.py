"""Unit tests for the zero-export controller's pure functions."""

import os
import sys
from pathlib import Path

os.environ.setdefault("HA_BASE_URL", "http://test")
os.environ.setdefault("HA_TOKEN", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

import pytest

from controller import (  # noqa: E402
    HAState,
    InverterState,
    TuningParams,
    compute_ceilings,
    compute_desired,
    distribute,
    effective_deadband,
    fetch_inverters,
)


def _params(**overrides) -> TuningParams:
    base = dict(
        target_w=-50.0,
        cap_w=800.0,
        per_max_w=600.0,
        loop_period_s=20.0,
        slow_approx=0.20,
        enabled=True,
    )
    base.update(overrides)
    return TuningParams(**base)


def _invs(reachable: tuple[bool, bool, bool]) -> list[InverterState]:
    return [
        InverterState(name=f"s{i+1}", power_w=0.0, reachable=r)
        for i, r in enumerate(reachable)
    ]


def _invs_p(*specs: tuple[str, float, bool]) -> list[InverterState]:
    """Build inverters with explicit (name, power_w, reachable) per spec."""
    return [InverterState(name=n, power_w=p, reachable=r) for n, p, r in specs]


def test_distribute_three_reachable_under_cap():
    out = distribute(800.0, _invs((True, True, True)), per_max_w=600.0)
    assert sum(out.values()) == 800.0
    assert all(0 < v <= 600 for v in out.values())
    assert max(out.values()) - min(out.values()) < 0.01  # equal share


def test_distribute_two_reachable():
    out = distribute(800.0, _invs((True, True, False)), per_max_w=600.0)
    assert out["s1"] == 400.0
    assert out["s2"] == 400.0
    assert out["s3"] == 0.0


def test_distribute_one_reachable_caps_at_per_max():
    out = distribute(800.0, _invs((False, False, True)), per_max_w=600.0)
    assert out["s3"] == 600.0
    assert out["s1"] == 0.0
    assert out["s2"] == 0.0
    assert sum(out.values()) <= 800.0


def test_distribute_none_reachable():
    out = distribute(800.0, _invs((False, False, False)), per_max_w=600.0)
    assert all(v == 0.0 for v in out.values())


def test_distribute_zero_desired():
    out = distribute(0.0, _invs((True, True, True)), per_max_w=600.0)
    assert all(v == 0.0 for v in out.values())


# compute_desired is feed-forward since v0.4.0: desired tracks estimated
# household consumption (grid + pv) instead of stepping incrementally from
# the previous limit. slow_approx is reused as the EMA smoothing factor.


def test_compute_desired_pins_at_cap_when_consumption_above_cap():
    # Household pulling 2000W from grid + 300W from PV = 2300W consumption.
    # Desired must jump straight to cap_w — not ramp toward it over many ticks.
    desired, ema = compute_desired(2000.0, 300.0, _params(slow_approx=1.0))
    assert desired == 800.0
    assert ema == 2300.0


def test_compute_desired_tracks_consumption_directly():
    # consumption = 100 + 250 = 350W; target 0 → produce exactly consumption.
    desired, _ = compute_desired(100.0, 250.0, _params(target_w=0.0, slow_approx=1.0))
    assert desired == 350.0


def test_compute_desired_negative_target_allows_feed_in():
    # target_w=-100 means "tolerate up to 100W export": desired = consumption + 100.
    desired, _ = compute_desired(0.0, 400.0, _params(target_w=-100.0, slow_approx=1.0))
    assert desired == 500.0


def test_compute_desired_single_step_down_on_consumption_drop():
    # Consumption was 800W (ema seeded), now drops to 300W. With alpha=1 the
    # desired lands at 300W in ONE tick — no multi-tick ramp-down.
    desired, ema = compute_desired(
        -200.0, 500.0, _params(target_w=0.0, slow_approx=1.0), prev_consumption_w=800.0
    )
    assert ema == 300.0
    assert desired == 300.0


def test_compute_desired_ema_smooths_consumption_spike():
    # Steady 500W consumption, one tick spikes to 2000W (kettle / sensor blip).
    # With alpha=0.3 the ema moves to 0.3*2000 + 0.7*500 = 950 — partial, not full.
    desired, ema = compute_desired(
        1700.0, 300.0, _params(target_w=0.0, slow_approx=0.3), prev_consumption_w=500.0
    )
    assert ema == pytest.approx(950.0)
    assert desired == pytest.approx(800.0)  # still clamped to cap


def test_compute_desired_first_call_seeds_ema_with_sample():
    # No previous estimate: the first sample IS the estimate (no warm-up lag).
    desired, ema = compute_desired(100.0, 200.0, _params(target_w=0.0, slow_approx=0.3))
    assert ema == 300.0
    assert desired == 300.0


def test_compute_desired_floors_at_zero():
    # Pathological: consumption estimate negative (meter glitch) → desired 0, not negative.
    desired, _ = compute_desired(-500.0, 100.0, _params(target_w=0.0, slow_approx=1.0))
    assert desired == 0.0


class _FakeHA:
    def __init__(self, states: dict[str, HAState | None]):
        self._states = states

    async def get_states(self, entity_ids):
        return {eid: self._states.get(eid) for eid in entity_ids}


def test_compute_ceilings_first_loop_with_steady_production():
    # First loop after pod restart with sun up: prev_limit defaults to per_max_w.
    # An inverter already producing at ~per_max_w looks limit-bound and gets
    # the full ceiling. At night (actual=0) the ceiling collapses to
    # SHADE_HEADROOM_W (100W), which is harmless because there's no power to
    # harvest.
    invs = _invs_p(("s2", 595.0, True), ("s3", 0.0, True), ("s1", 0.0, False))
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits={})
    assert ceilings["s2"] == 650.0          # producing near per_max → full
    assert ceilings["s3"] == 100.0          # actual=0 → tight cap (night-safe)
    assert ceilings["s1"] == 0.0            # unreachable


def test_compute_ceilings_sun_limited_gets_tight_cap():
    # s2 producing close to its 400 W limit (limit-bound) → full ceiling.
    # s3 producing only 100 W against a 400 W limit (sun-limited) → tight ceiling.
    invs = _invs_p(("s1", 0.0, False), ("s2", 395.0, True), ("s3", 100.0, True))
    last = {"s2": 400.0, "s3": 400.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    assert ceilings["s2"] == 650.0           # limit-bound -> raise ceiling
    assert ceilings["s3"] == 100.0 + 100.0   # sun-limited -> actual + headroom
    assert ceilings["s1"] == 0.0             # unreachable


def test_compute_ceilings_recovery_when_actual_meets_limit():
    # Yesterday s3 was shaded; we tightened limit to 200. Now sun is back and
    # s3 is producing at the 200 W limit. It must look limit-bound so the
    # next ceiling restores to per_max_w and harvest can grow.
    invs = _invs_p(("s2", 600.0, True), ("s3", 195.0, True), ("s1", 0.0, False))
    last = {"s2": 650.0, "s3": 200.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    assert ceilings["s3"] == 650.0  # 195+30=225 not < 200 -> not sun-limited


def test_compute_ceilings_holds_prev_when_power_unavailable():
    # Regression: OpenDTU's MQTT brownouts every 1-2 minutes left
    # sensor.s2_power=unavailable while binary_sensor.s2_reachable stayed on.
    # Old behavior coerced power_w to 0.0 → shade branch → ceiling crashed to
    # SHADE_HEADROOM_W (100 W), oscillating the limit every tick.
    # Fix: power_w=None must hold the prev limit so the deadband suppresses
    # the write entirely.
    invs = _invs_p(("s2", None, True), ("s3", 345.0, True), ("s1", 0.0, False))
    last = {"s2": 360.0, "s3": 360.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    assert ceilings["s2"] == 360.0   # held at prev — no shade decision possible
    assert ceilings["s3"] == 650.0   # 345+30=375 not <360 -> limit-bound
    assert ceilings["s1"] == 0.0


def test_compute_ceilings_unavailable_with_no_prev_holds_per_max():
    # First loop after pod restart with no last_limits and the power sensor
    # already unavailable: hold the default (per_max_w) rather than 0.
    invs = _invs_p(("s2", None, True),)
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits={})
    assert ceilings["s2"] == 650.0   # last_limits.get default = per_max_w


def test_compute_ceilings_clamps_to_per_max():
    # Sun-limited inverter actual + SHADE_HEADROOM_W could exceed per_max_w
    # in theory; the ceiling must never exceed the hardware cap.
    invs = _invs_p(("s2", 580.0, True), ("s3", 0.0, False), ("s1", 0.0, False))
    last = {"s2": 700.0}  # we previously requested above per_max_w (e.g. helper bumped down)
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    # 580+100=680 > 650 → clamped to 650
    assert ceilings["s2"] == 650.0


def test_compute_ceilings_hysteresis_holds_ceiling_through_pv_wobble():
    # Regression (2026-09): with all three inverters sun-limited under broken
    # cloud, the ceiling was recomputed from instantaneous power every tick, so
    # every PV wobble above the deadband became a limit write — ~319 writes/day
    # against a v0.4.0 design goal of ~12. Production moving inside the
    # hysteresis band must leave the ceiling untouched so the deadband can
    # suppress the write entirely.
    last = {"s2": 250.0}
    for power in (150.0, 170.0, 130.0, 120.0, 160.0):
        invs = _invs_p(("s2", power, True),)
        ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
        assert ceilings["s2"] == 250.0, f"ceiling moved on {power} W wobble"


def test_compute_ceilings_hysteresis_releases_upward_via_limit_bound():
    # The upward path needs no separate re-target: production climbing to
    # within SHADE_MARGIN_W of the held ceiling trips the existing limit-bound
    # branch, which restores the full per_max_w ceiling in one step. Holding
    # below that point is safe because the inverter still has room to grow.
    invs = _invs_p(("s2", 225.0, True),)   # headroom 250-225 = 25 <= SHADE_MARGIN_W
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits={"s2": 250.0})
    assert ceilings["s2"] == 650.0


def test_compute_ceilings_hysteresis_releases_when_production_collapses():
    # A cloud bank drops production far below the held ceiling: once headroom
    # exceeds the band the ceiling must follow it down, so the water-fill frees
    # that allocation for a productive inverter.
    invs = _invs_p(("s2", 100.0, True),)    # headroom 450-100 = 350 > 100+120
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits={"s2": 450.0})
    assert ceilings["s2"] == 200.0          # 100 + SHADE_HEADROOM_W


def test_compute_ceilings_hysteresis_never_exceeds_per_max():
    # The held ceiling must be clamped to per_max_w even when the previous
    # limit sat above it (e.g. the per-inverter helper was just lowered).
    invs = _invs_p(("s2", 560.0, True),)    # held 650, headroom 90 -> re-target
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits={"s2": 900.0})
    assert ceilings["s2"] == 650.0


def test_compute_ceilings_hysteresis_preserves_cap_invariant():
    # Holding a stale (higher) ceiling must never let the distributed sum
    # exceed the legal cap — distribute() is still bounded by `desired`.
    invs = _invs_p(("s1", 150.0, True), ("s2", 160.0, True), ("s3", 140.0, True))
    last = {"s1": 260.0, "s2": 270.0, "s3": 250.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    assert ceilings == last                  # all inside the band -> all held
    limits = distribute(900.0, invs, per_max_w=650.0, ceilings=ceilings)
    assert sum(limits.values()) <= 900.0


def test_distribute_east_west_partial_shade_redistributes_headroom():
    # The headline scenario: s2 is east-facing in afternoon producing close to
    # its 650 W cap, s3 is west-facing producing only 100 W. With desired=900
    # we want s2 at 650 (full hardware) and s3 at 200 (tight cap), summing to
    # 850 W ≤ 900 cap. Naive equal-share would give {s2:450, s3:450} and lose
    # ~250 W of harvest.
    invs = _invs_p(("s2", 645.0, True), ("s3", 100.0, True))
    last = {"s2": 650.0, "s3": 650.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    limits = distribute(900.0, invs, per_max_w=650.0, ceilings=ceilings)
    assert limits["s2"] == 650.0       # productive inverter at hardware cap
    assert limits["s3"] == 200.0       # shaded inverter capped near actual
    assert sum(limits.values()) <= 900.0  # legal cap respected


def test_distribute_redistributes_to_one_when_other_shaded_low_desired():
    # Lower desired (e.g. 500 W). Shaded s3 takes its 200, leaving 300 to s2.
    invs = _invs_p(("s2", 295.0, True), ("s3", 100.0, True))
    last = {"s2": 300.0, "s3": 650.0}
    ceilings = compute_ceilings(invs, per_max_w=650.0, last_limits=last)
    limits = distribute(500.0, invs, per_max_w=650.0, ceilings=ceilings)
    assert limits["s2"] == 300.0
    assert limits["s3"] == 200.0
    assert sum(limits.values()) == 500.0


def test_distribute_no_ceilings_arg_falls_back_to_per_max():
    # Backwards-compat path used by callers that don't compute ceilings.
    invs = _invs((True, True, True))
    limits = distribute(800.0, invs, per_max_w=600.0)
    assert sum(limits.values()) == 800.0
    assert all(v <= 600.0 for v in limits.values())


def test_effective_deadband_normal_when_grid_below_threshold():
    # Grid near target → tight deadband for precise control.
    assert effective_deadband(grid_w=-30.0, threshold_w=400.0, base_w=5.0, saturated_w=50.0) == 5.0
    assert effective_deadband(grid_w=200.0, threshold_w=400.0, base_w=5.0, saturated_w=50.0) == 5.0
    assert effective_deadband(grid_w=400.0, threshold_w=400.0, base_w=5.0, saturated_w=50.0) == 5.0


def test_effective_deadband_widens_in_saturated_import():
    # Grid well above threshold → wider deadband to suppress write churn.
    assert effective_deadband(grid_w=401.0, threshold_w=400.0, base_w=5.0, saturated_w=50.0) == 50.0
    assert effective_deadband(grid_w=2000.0, threshold_w=400.0, base_w=5.0, saturated_w=50.0) == 50.0


@pytest.mark.asyncio
async def test_fetch_inverters_reachable_ignores_binary_sensor_staleness():
    # Regression: binary_sensor.*_reachable updates last_updated only on
    # transitions; a stable-on sensor 14 minutes old must still count as
    # reachable. Bug surfaced during dry-run deployment 2026-05-09.
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=14)
    fresh = datetime.now(timezone.utc)
    fake = _FakeHA(
        {
            "sensor.s1_power": HAState(state="412.0", last_updated=fresh),
            "binary_sensor.s1_reachable": HAState(state="on", last_updated=long_ago),
            "sensor.s2_power": HAState(state="0.0", last_updated=fresh),
            "binary_sensor.s2_reachable": HAState(state="off", last_updated=long_ago),
            "sensor.s3_power": HAState(state="0.0", last_updated=fresh),
            "binary_sensor.s3_reachable": HAState(state="unavailable", last_updated=fresh),
        }
    )
    invs = await fetch_inverters(fake, ["s1", "s2", "s3"])
    by_name = {i.name: i for i in invs}
    assert by_name["s1"].reachable is True   # stale-but-on -> reachable
    assert by_name["s2"].reachable is False  # explicitly off
    assert by_name["s3"].reachable is False  # unavailable
    assert by_name["s1"].power_w == 412.0


@pytest.mark.asyncio
async def test_fetch_inverters_unavailable_power_is_none_not_zero():
    # Regression: sensor.s_n_power=unavailable used to surface as power_w=0.0,
    # which falsely tripped shade detection. It must surface as None so
    # compute_ceilings can hold the prev limit.
    fresh = datetime.now(timezone.utc)
    fake = _FakeHA(
        {
            "sensor.s1_power": HAState(state="unavailable", last_updated=fresh),
            "binary_sensor.s1_reachable": HAState(state="on", last_updated=fresh),
        }
    )
    invs = await fetch_inverters(fake, ["s1"])
    assert invs[0].power_w is None
    assert invs[0].reachable is True
