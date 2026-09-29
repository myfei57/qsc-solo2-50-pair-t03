"""蒸汽与开合模：排气、升温、泄压、开模的硬顺序。"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError, SequenceError
from rubbercure.lift.open import open_mold
from rubbercure.lift.position import ClampPosition
from rubbercure.lift.segment import ClampSegment, segment_for_opening
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


def test_close_segments_partition_the_stroke():
    assert segment_for_opening(100.0) is ClampSegment.FAST
    assert segment_for_opening(87.5) is ClampSegment.FAST
    assert segment_for_opening(75.0) is ClampSegment.SLOW
    assert segment_for_opening(62.5) is ClampSegment.SLOW
    assert segment_for_opening(50.0) is ClampSegment.GUARD
    assert segment_for_opening(25.0) is ClampSegment.LOCK
    assert segment_for_opening(0.0) is ClampSegment.LOCK


@pytest.mark.parametrize(
    "stop_at,segment_label,expect_phase",
    [
        (87.5, "快合段", LiftPhase.CLOSING),
        (62.5, "慢合段", LiftPhase.CLOSING),
        (37.5, "护模低压段", LiftPhase.CLOSING),
        (12.5, "锁模高压段", LiftPhase.CLOSING),
    ],
)
def test_jammed_close_reports_segment_and_never_reports_in_position(
    platform, stop_at, segment_label, expect_phase
):
    unit = platform.press("press-01")
    unit.load_mold("mold-J")
    with pytest.raises(InterlockError) as excinfo:
        unit.lift.close_clamp("mold-J", stop_at_opening_pct=stop_at)
    assert segment_label in excinfo.value.message
    # 卡住后相位停在 closing，行程与力都不能被当成到位
    assert unit.lift.phase is expect_phase
    assert not unit.lift.is_closed()
    assert not unit.lift.is_locked()
    assert unit.lift.clamp_force_kn() < unit.lift.required_force_kn
    with pytest.raises(InterlockError):
        unit.lift.require_locked()


def test_successful_close_issues_lock_evidence_by_force(platform):
    unit = platform.press("press-01")
    token = unit.lift.close_clamp("mold-L")
    assert unit.lift.is_locked()
    assert token.is_valid()
    assert token.for_mold("mold-L")
    assert token.clamp_force_kn >= token.required_kn
    assert token.segment == ClampSegment.LOCK.value
    assert unit.lift.require_locked() is token


def test_open_requires_lock_evidence_for_same_mold(platform):
    unit = platform.press("press-01")
    # 从没锁上过：拒绝开模
    with pytest.raises(InterlockError):
        unit.lift.open_clamp("mold-N")
    unit.lift.close_clamp("mold-N")
    # 凭据钉在模具上，不能串模
    with pytest.raises(InterlockError):
        unit.lift.require_token_for("mold-other")
    assert unit.lift.open_clamp("mold-N") == 100.0
    # 开模完成凭据注销，不能重复使用
    assert not unit.lift.is_locked()
    with pytest.raises(InterlockError):
        unit.lift.open_clamp("mold-N")


def test_jammed_close_can_retract_and_retry(platform):
    unit = platform.press("press-01")
    unit.load_mold("mold-R")
    with pytest.raises(InterlockError):
        unit.lift.close_clamp("mold-R", stop_at_opening_pct=37.5)
    # 非合模相位不允许退模
    other = platform.press("press-02")
    with pytest.raises(SequenceError):
        other.lift.retract_clamp("mold-R")
    opening = unit.lift.retract_clamp("mold-R")
    assert opening == 100.0
    assert unit.lift.phase is LiftPhase.OPEN
    assert not unit.lift.is_locked()
    # 阻挡是一次性动作参数，重试不再带阻挡即正常锁上
    token = unit.lift.close_clamp("mold-R")
    assert token.is_valid()


def test_open_mold_rejects_when_lock_evidence_missing(platform):
    unit = platform.press("press-01")
    unit.load_mold("mold-V")
    safe = VentToken(
        mold_id="mold-V",
        residual_kpa=SAFE_RESIDUAL_KPA,
        vented_at=platform.clock.now(),
        safe_below_kpa=SAFE_RESIDUAL_KPA,
        hold_s=8.0,
    )
    # 泄压凭据合格，但没有锁模凭据，一样禁止开模
    with pytest.raises(InterlockError):
        open_mold(unit.lift, safe, platform.bus, "mold-V")


def test_close_clamp_rejects_out_of_range_stop_position(platform):
    unit = platform.press("press-01")
    for bad in (0.0, -10.0, 140.0):
        with pytest.raises(ValueError):
            unit.lift.close_clamp("mold-B", stop_at_opening_pct=bad)
