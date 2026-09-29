"""开合模本体。

合模与开模是两条方向相反的动作，但它们共享同一套行程状态。**合模到位不
再以行程为据**：行程开关只能说明动板走到了某个位置，模具里夹了异物、机构
磨损卡顿时，半截行程也可能给出到位信号。现在合模按分段推进并监测卡顿，
走到锁模位后还必须建立起达标锁模力，本体才签发 :class:`ClampToken`；升温、
计时等下游动作一律凭凭据放行。

合模全程分四段（开度自 100% 向 0%）：快速合模段、低压护模段、高压锁模段、
锁模位。卡顿时按当前开度报出具体段名与锁模力，维修可直接按段排查。开模
前必须出示锁模凭据并当场复测锁模力，确认这一模确实锁过且没有掉压。
"""

from __future__ import annotations

import threading

from ..clock import Clock, SystemClock
from ..errors import InterlockError, SequenceError
from ..eventbus import Event, EventBus
from .lock import ClampToken
from .position import (
    CLOSED_PCT,
    FULLY_OPEN_PCT,
    HIGH_PRESS_START_PCT,
    LOW_PRESS_START_PCT,
    ClampPosition,
    segment_for_opening,
)
from .state import LiftPhase, LiftStateMachine

TRAVEL_STEP_PCT = 25.0

# 各分段建立的锁模力区间（kN）：空程基本无负载，护模段低压试探，
# 锁模段才把力建到工艺吨位。
MIN_CLAMP_FORCE_KN = 180.0
LOW_PRESS_FORCE_KN = 320.0
HIGH_PRESS_FORCE_KN = 900.0
MAX_CLAMP_FORCE_KN = 1450.0

# 锁模到位的唯一判据：锁模力必须达到该下限才签发凭据。
LOCK_FORCE_MIN_KN = 1300.0


class LiftUnit:
    """硫化机开合模本体：行程分段、锁模力与模具承载。"""

    def __init__(
        self,
        bus: EventBus,
        step_pct: float = TRAVEL_STEP_PCT,
        clock: Clock | None = None,
    ) -> None:
        self._bus = bus
        self._step = float(step_pct)
        if self._step <= 0:
            raise ValueError("行程步长必须为正")
        self._clock = clock or SystemClock()
        self._state = LiftStateMachine()
        self._position = ClampPosition(FULLY_OPEN_PCT)
        self._carried_mold: str | None = None
        self._lock_token: ClampToken | None = None
        self._lock_seq = 0
        self._lock = threading.RLock()

    @property
    def opening_pct(self) -> float:
        return self._position.opening_pct

    @property
    def phase(self) -> LiftPhase:
        return self._state.phase

    @property
    def carried_mold(self) -> str | None:
        return self._carried_mold

    def clamp_force_kn(self, lockup_factor: float = 1.0) -> float:
        """按当前行程分段折算锁模力。

        快速段只有背压；低压护模段保持低压试探，防止夹伤模具与制品；进入
        高压锁模段后锁模力快速爬升，锁模位达到额定吨位。
        ``lockup_factor`` 模拟锁模机构建力效率（磨损／内泄时小于 1，行程
        到位也建不起达标锁模力）。
        """

        opening = self._position.opening_pct
        if opening <= CLOSED_PCT:
            force = MAX_CLAMP_FORCE_KN
        elif opening <= HIGH_PRESS_START_PCT:
            ratio = (HIGH_PRESS_START_PCT - opening) / HIGH_PRESS_START_PCT
            force = HIGH_PRESS_FORCE_KN + (MAX_CLAMP_FORCE_KN - HIGH_PRESS_FORCE_KN) * ratio
        elif opening <= LOW_PRESS_START_PCT:
            span = LOW_PRESS_START_PCT - HIGH_PRESS_START_PCT
            ratio = (LOW_PRESS_START_PCT - opening) / span
            force = LOW_PRESS_FORCE_KN + (HIGH_PRESS_FORCE_KN - LOW_PRESS_FORCE_KN) * ratio
        else:
            span = FULLY_OPEN_PCT - LOW_PRESS_START_PCT
            ratio = (FULLY_OPEN_PCT - opening) / span
            force = MIN_CLAMP_FORCE_KN + (LOW_PRESS_FORCE_KN - MIN_CLAMP_FORCE_KN) * ratio
        return force * lockup_factor

    def is_closed(self) -> bool:
        return self._position.is_closed()

    def is_open(self) -> bool:
        return self._position.is_open()

    def lock_token(self) -> ClampToken | None:
        """返回当前在机模具的锁模到位凭据（未锁或已开模为 None）。"""

        return self._lock_token

    def is_locked(self) -> bool:
        """锁模凭据有效且当场复测锁模力仍达标，才算真的锁到位。"""

        token = self._lock_token
        return (
            token is not None
            and self._state.phase is LiftPhase.CLOSED
            and token.covers(
                token.mold_id,
                live_force_kn=self.clamp_force_kn(),
                opening_pct=self.opening_pct,
            )
        )

    def require_locked(self, component: str = "lift") -> ClampToken:
        """下游动作（升温、计时、开模前复核）的强制联锁：必须锁模到位。"""

        token = self._lock_token
        if token is None:
            raise InterlockError(
                f"模具 {self._carried_mold or '?'} 没有锁模到位凭据，禁止下游动作",
                component=component,
            )
        if self._state.phase is not LiftPhase.CLOSED:
            raise InterlockError(
                f"模具 {token.mold_id} 当前相位 {self._state.phase.value}，不是锁模到位状态",
                component=component,
            )
        live_force = self.clamp_force_kn()
        if not token.covers(
            token.mold_id, live_force_kn=live_force, opening_pct=self.opening_pct
        ):
            raise InterlockError(
                f"模具 {token.mold_id} 锁模凭据复核失败"
                f"（行程 {self.opening_pct:.1f}%，实测锁模力 {live_force:.1f} kN"
                f" < {LOCK_FORCE_MIN_KN:.0f} kN），禁止下游动作",
                component=component,
            )
        return token

    def _drive(self, target_pct: float, stop_pct: float | None = None) -> float:
        """按步长把行程推到目标位置；被 ``stop_pct`` 挡住走不动即判定卡顿。"""

        while True:
            current = self._position.opening_pct
            if abs(current - target_pct) <= 1e-9:
                return current
            nxt = self._position.approach(target_pct, self._step)
            # 故障注入：合模方向上行程不允许越过 stop_pct，模拟机械卡滞。
            if stop_pct is not None and target_pct < current:
                nxt = ClampPosition(max(nxt.opening_pct, stop_pct))
            if nxt.opening_pct == current:
                segment = segment_for_opening(current)
                force = self.clamp_force_kn()
                raise InterlockError(
                    f"合模卡在{segment.label}（行程 {current:.1f}%，"
                    f"当前锁模力 {force:.1f} kN），未发出锁模到位凭据",
                    component="lift",
                )
            self._position = nxt

    def close_clamp(
        self,
        mold_id: str,
        *,
        jam_at_pct: float | None = None,
        lockup_factor: float = 1.0,
    ) -> ClampToken:
        """合模并建锁模力，达标后签发锁模到位凭据。

        行程走不动（机械卡顿）或到锁模位后建不起达标锁模力，都拒绝判到位，
        相位停在 ``closing`` 并发告警事件，凭据绝不签发。
        """

        with self._lock:
            self._state.transition(LiftPhase.CLOSING)
            self._bus.publish(
                Event.of("lift", "lift", "close_start", mold_id=mold_id, opening_pct=self.opening_pct)
            )
            self._carried_mold = mold_id
            try:
                opening = self._drive(CLOSED_PCT, stop_pct=jam_at_pct)
            except InterlockError as error:
                self._bus.publish(
                    Event.of(
                        "lift",
                        "lift",
                        "close_jam",
                        severity="alarm",
                        mold_id=mold_id,
                        opening_pct=round(self.opening_pct, 3),
                        segment=segment_for_opening(self.opening_pct).value,
                        clamp_force_kn=round(self.clamp_force_kn(lockup_factor), 2),
                        message=error.message,
                    )
                )
                raise
            force = self.clamp_force_kn(lockup_factor)
            if not self._position.is_closed() or force < LOCK_FORCE_MIN_KN:
                self._bus.publish(
                    Event.of(
                        "lift",
                        "lift",
                        "lock_force_short",
                        severity="alarm",
                        mold_id=mold_id,
                        opening_pct=round(opening, 3),
                        clamp_force_kn=round(force, 2),
                        required_force_kn=LOCK_FORCE_MIN_KN,
                    )
                )
                raise InterlockError(
                    f"模具 {mold_id} 行程已到锁模位但锁模力仅 {force:.1f} kN"
                    f"（要求 ≥ {LOCK_FORCE_MIN_KN:.0f} kN），不判到位、禁止升温",
                    component="lift",
                )
            self._lock_seq += 1
            token = ClampToken(
                token_id=f"LOCK-{mold_id}-{self._lock_seq:04d}",
                mold_id=mold_id,
                opening_pct=opening,
                clamp_force_kn=force,
                locked_at=self._clock.now(),
                required_force_kn=LOCK_FORCE_MIN_KN,
            )
            self._lock_token = token
            self._state.transition(LiftPhase.CLOSED)
        self._bus.publish(
            Event.of(
                "lift",
                "lift",
                "close_done",
                segment=segment_for_opening(opening).value,
                **token.as_dict(),
            )
        )
        return token

    def open_clamp(self, mold_id: str) -> float:
        """开模到取件位（原语）。

        业务侧必须先由 :func:`rubbercure.lift.open.open_mold` 校验泄压凭据与
        锁模凭据；本体只核对在机模具身份并收回锁模凭据。
        """

        with self._lock:
            if self._carried_mold is not None and self._carried_mold != mold_id:
                raise SequenceError(
                    f"开模对象 {mold_id} 与在机模具 {self._carried_mold} 不一致",
                    component="lift",
                )
            self._state.transition(LiftPhase.OPENING)
            self._bus.publish(
                Event.of("lift", "lift", "open_start", mold_id=mold_id, opening_pct=self.opening_pct)
            )
            opening = self._drive(FULLY_OPEN_PCT)
            self._state.transition(LiftPhase.OPEN)
            self._carried_mold = None
            self._lock_token = None
        self._bus.publish(
            Event.of("lift", "lift", "open_done", mold_id=mold_id, opening_pct=opening)
        )
        return opening

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._state.phase.value,
                "history": self._state.history(),
                "position": self._position.as_dict(),
                "clamp_force_kn": round(self.clamp_force_kn(), 2),
                "locked": self.is_locked(),
                "lock_token": self._lock_token.as_dict() if self._lock_token else None,
                "carried_mold": self._carried_mold,
            }
