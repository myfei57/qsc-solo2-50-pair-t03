"""蒸汽与开合模：排气、升温、泄压、开模的硬顺序。"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError, SequenceError
from rubbercure.lift.lift import LOCK_FORCE_MIN_KN
from rubbercure.lift.lock import ClampToken
from rubbercure.lift.open import open_mold
from rubbercure.lift.position import (
    HIGH_PRESS_START_PCT,
    LOW_PRESS_START_PCT,
    ClampPosition,
    TravelSegment,
    segment_for_opening,
)
from rubbercure.lift.state import LiftPhase, LiftStateMachine
from rubbercure.steam.vent import SAFE_RESIDUAL_KPA, VentToken


def test_vent_before_open_is_enforced(platform):
    unit = platform.press("press-01")
    unit.load_mold("mold-V")
    unit.lift.close_clamp("mold-V")
    unsafe = VentToken(
        mold_id="mold-V",
        residual_kpa=200.0,
        vented_at=platform.clock.now(),
        safe_below_kpa=SAFE_RESIDUAL_KPA,
        hold_s=8.0,
    )
    assert not unsafe.is_safe()
    with pytest.raises(InterlockError):
        open_mold(unit.lift, unsafe, platform.bus, "mold-V")


def test_vent_drops_pressure_below_safe_threshold(platform):
    unit = platform.press("press-01")
    unit.steam.header.charge(900.0, 30.0)
    assert unit.steam.pressure_kpa > SAFE_RESIDUAL_KPA
    token = unit.steam.vent_pressure("mold-V")
    assert token.is_safe()
    assert unit.steam.pressure_kpa <= SAFE_RESIDUAL_KPA
    assert unit.steam.vent_token() is token


def test_purge_requires_main_valve_closed(platform):
    unit = platform.press("press-01")
    unit.steam.set_valve(60.0)
    with pytest.raises(InterlockError):
        unit.steam.purge_cavity("mold-P", generation=1)


def test_clamp_position_never_overshoots_target():
    position = ClampPosition(100.0)
    steps = []
    while not position.is_closed():
        position = position.approach(0.0, 30.0)
        steps.append(position.opening_pct)
    assert steps[-1] == 0.0
    assert steps == sorted(steps, reverse=True)
    assert ClampPosition(0.0).is_closed()
    assert ClampPosition(100.0).is_open()


def test_clamp_position_rejects_out_of_range():
    with pytest.raises(ValueError):
        ClampPosition(140.0)


def test_lift_state_machine_rejects_illegal_transition():
    machine = LiftStateMachine()
    assert machine.phase is LiftPhase.OPEN
    with pytest.raises(SequenceError):
        machine.transition(LiftPhase.OPENING)
    machine.transition(LiftPhase.CLOSING)
    assert machine.history() == ["open", "closing"]


def test_travel_segments_classify_each_closing_stage():
    assert segment_for_opening(100.0) is TravelSegment.FAST
    assert segment_for_opening(LOW_PRESS_START_PCT + 1.0) is TravelSegment.FAST
    assert segment_for_opening(LOW_PRESS_START_PCT) is TravelSegment.LOW_PRESS
    assert segment_for_opening(HIGH_PRESS_START_PCT + 1.0) is TravelSegment.LOW_PRESS
    assert segment_for_opening(HIGH_PRESS_START_PCT) is TravelSegment.HIGH_PRESS
    assert segment_for_opening(0.0) is TravelSegment.LOCKED
    assert ClampPosition(8.0).segment().label == "高压锁模段"


def test_successful_close_issues_lock_token_on_force_not_stroke(platform):
    unit = platform.press("press-01")
    token = unit.lift.close_clamp("mold-L")
    assert token.clamp_force_kn >= LOCK_FORCE_MIN_KN
    assert unit.lift.is_locked()
    assert unit.lift.lock_token() is token
    assert unit.lift.snapshot()["locked"] is True
    done = next(ev for ev in platform.bus.history("lift") if ev.action == "close_done")
    assert done.payload["token_id"] == token.token_id
    assert done.payload["segment"] == "locked"


@pytest.mark.parametrize(
    ("jam_at_pct", "segment", "label"),
    [
        (55.0, "fast", "快速合模段"),
        (30.0, "low_press", "低压护模段"),
        (8.0, "high_press", "高压锁模段"),
    ],
)
def test_close_jam_is_reported_with_the_stalled_segment(platform, jam_at_pct, segment, label):
    unit = platform.press("press-01")
    with pytest.raises(InterlockError) as excinfo:
        unit.lift.close_clamp("mold-J", jam_at_pct=jam_at_pct)
    assert label in excinfo.value.message
    assert unit.lift.phase is LiftPhase.CLOSING
    assert unit.lift.lock_token() is None
    jam = next(ev for ev in platform.bus.history("lift") if ev.action == "close_jam")
    assert jam.payload["severity"] == "alarm"
    assert jam.payload["segment"] == segment
    assert jam.payload["opening_pct"] == jam_at_pct


def test_close_at_position_without_lock_force_is_not_closed(platform):
    unit = platform.press("press-01")
    with pytest.raises(InterlockError) as excinfo:
        unit.lift.close_clamp("mold-W", lockup_factor=0.8)
    assert "锁模力" in excinfo.value.message
    assert unit.lift.phase is LiftPhase.CLOSING
    assert not unit.lift.is_locked()
    short = next(
        ev for ev in platform.bus.history("lift") if ev.action == "lock_force_short"
    )
    assert short.payload["clamp_force_kn"] < LOCK_FORCE_MIN_KN
    assert short.payload["severity"] == "alarm"


def _safe_vent(platform, mold_id: str) -> VentToken:
    return VentToken(
        mold_id=mold_id,
        residual_kpa=5.0,
        vented_at=platform.clock.now(),
        safe_below_kpa=SAFE_RESIDUAL_KPA,
        hold_s=8.0,
    )


def test_open_without_lock_token_is_refused(platform):
    unit = platform.press("press-01")
    unit.load_mold("mold-N")
    with pytest.raises(InterlockError) as excinfo:
        open_mold(unit.lift, _safe_vent(platform, "mold-N"), platform.bus, "mold-N")
    assert "锁模到位凭据" in excinfo.value.message


def test_open_rejects_lock_token_belonging_to_another_mold(platform):
    unit = platform.press("press-01")
    unit.lift.close_clamp("mold-A")
    foreign = ClampToken(
        token_id="LOCK-mold-B-0001",
        mold_id="mold-B",
        opening_pct=0.0,
        clamp_force_kn=1400.0,
        locked_at=platform.clock.now(),
        required_force_kn=LOCK_FORCE_MIN_KN,
    )
    with pytest.raises(InterlockError) as excinfo:
        open_mold(unit.lift, _safe_vent(platform, "mold-A"), platform.bus, "mold-A", lock=foreign)
    assert "mold-B" in excinfo.value.message


def test_open_consumes_lock_token_and_records_evidence(platform):
    unit = platform.press("press-01")
    token = unit.lift.close_clamp("mold-O")
    result = open_mold(unit.lift, _safe_vent(platform, "mold-O"), platform.bus, "mold-O")
    assert result["lock_token_id"] == token.token_id
    assert result["locked_force_kn"] >= LOCK_FORCE_MIN_KN
    assert unit.lift.lock_token() is None
    opened = next(ev for ev in platform.bus.history("lift") if ev.action == "mold_opened")
    assert opened.payload["lock_token_id"] == token.token_id
    # 凭据已随开模收回，拿旧凭据不能再开第二次
    with pytest.raises(InterlockError):
        open_mold(unit.lift, _safe_vent(platform, "mold-O"), platform.bus, "mold-O", lock=token)
