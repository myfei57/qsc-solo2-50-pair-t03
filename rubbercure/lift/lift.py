"""开合模本体。

合模与开模是两条方向相反的动作，但它们共享同一套行程状态。**到位判定以
锁模力为准，不以行程为准**：行程开关可能在模具卡了半截时就报到位，而锁模
力造不了假——行程没走完，锁模力一定上不来。合模驱动按快合、慢合、护模、
锁模四段推进，若中途被卡住则停在卡住的那段并指出段名，拒绝把相位推进到
已闭合；只有锁模力达到 :data:`REQUIRED_CLAMP_FORCE_KN` 才签发锁模凭据
（:class:`~rubbercure.lift.token.ClampToken`）。硫化升温、计时凭凭据放行，
开模前必须出示同模次的有效凭据，凭据在开模完成后注销。开模前还必须确认
蒸汽已泄压、模具内无残余压力，否则制品会被顶裂。行程推进按固定步长分段，
验证环境里可以逐段观察中间位置，也可以用 ``stop_at_opening_pct`` 注入机械
阻挡来复现卡模。
"""

from __future__ import annotations

import threading

from ..clock import Clock, SystemClock
from ..errors import InterlockError, SequenceError
from ..eventbus import Event, EventBus
from .position import CLOSED_PCT, FULLY_OPEN_PCT, ClampPosition
from .segment import ClampSegment, segment_for_opening
from .state import LiftPhase, LiftStateMachine
from .token import ClampToken

TRAVEL_STEP_PCT = 25.0
MIN_CLAMP_FORCE_KN = 180.0
MAX_CLAMP_FORCE_KN = 1450.0
#: 锁模到位的锁模力下限：达到才签发锁模凭据。
REQUIRED_CLAMP_FORCE_KN = 1400.0


class LiftUnit:
    """硫化机开合模本体：行程、锁模力、卡模注入与锁模凭据。"""

    def __init__(
        self,
        bus: EventBus,
        step_pct: float = TRAVEL_STEP_PCT,
        clock: Clock | None = None,
        required_force_kn: float = REQUIRED_CLAMP_FORCE_KN,
    ) -> None:
        self._bus = bus
        self._step = float(step_pct)
        if self._step <= 0:
            raise ValueError("行程步长必须为正")
        self._clock = clock or SystemClock()
        self._required_force_kn = float(required_force_kn)
        if not MIN_CLAMP_FORCE_KN <= self._required_force_kn <= MAX_CLAMP_FORCE_KN:
            raise ValueError("锁模力到位阈值超出力传感器量程")
        self._state = LiftStateMachine()
        self._position = ClampPosition(FULLY_OPEN_PCT)
        self._carried_mold: str | None = None
        self._clamp_token: ClampToken | None = None
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

    @property
    def required_force_kn(self) -> float:
        return self._required_force_kn

    def clamp_force_kn(self) -> float:
        """按当前行程折算锁模力：越接近全闭，锁模力越大。"""

        closure = 1.0 - self._position.opening_pct / FULLY_OPEN_PCT
        return MIN_CLAMP_FORCE_KN + (MAX_CLAMP_FORCE_KN - MIN_CLAMP_FORCE_KN) * closure

    def is_closed(self) -> bool:
        """行程是否走到全闭位（仅描述行程，不代表锁模力达标）。"""

        return self._position.is_closed()

    def is_open(self) -> bool:
        return self._position.is_open()

    def is_locked(self) -> bool:
        """是否持有有效锁模凭据——唯一的“合模到位”判据。"""

        token = self._clamp_token
        return token is not None and token.is_valid()

    def clamp_token(self) -> ClampToken | None:
        """返回当前有效的锁模凭据；没有则为 ``None``。"""

        return self._clamp_token if self.is_locked() else None

    def require_locked(self, component: str = "lift") -> ClampToken:
        """要求本模已凭锁模力确认到位，返回锁模凭据；否则拒绝下游动作。"""

        token = self._clamp_token
        if token is None or not token.is_valid():
            raise InterlockError(
                "锁模力未达标、缺少有效锁模凭据（当前"
                f" {self.clamp_force_kn():.1f}/{self._required_force_kn:.0f} kN，"
                f"行程 {self.opening_pct:.1f}%），禁止下游动作",
                component=component,
            )
        return token

    def require_token_for(self, mold_id: str, component: str = "lift") -> ClampToken:
        """开模前置校验：出示属于本模次的有效锁模凭据。"""

        token = self._clamp_token
        if token is None:
            raise InterlockError(
                f"模具 {mold_id} 没有锁模到位凭据，无法确认本模锁上，禁止开模",
                component=component,
            )
        if not token.for_mold(mold_id):
            raise InterlockError(
                f"锁模凭据属于模具 {token.mold_id}，与待开模具 {mold_id} 不一致，禁止开模",
                component=component,
            )
        if not token.is_valid():
            raise InterlockError(
                f"模具 {mold_id} 的锁模凭据已失效，禁止开模",
                component=component,
            )
        return token

    def _drive_close(
        self, stop_at_opening_pct: float | None
    ) -> tuple[float, ClampSegment | None]:
        """合模方向按步长推进，遇阻挡则停住，返回 (终点开度量, 卡住段)。

        ``stop_at_opening_pct`` 表达本次合模动作观测到的机械阻挡位（导轨异物、
        分型面带胶等）：驱动闭到该开度量即停；为 ``None`` 时一直闭到全闭位。
        """

        target = CLOSED_PCT
        blocked_at: float | None = None
        if stop_at_opening_pct is not None:
            stop = float(stop_at_opening_pct)
            if not CLOSED_PCT < stop <= FULLY_OPEN_PCT:
                raise ValueError("阻挡位置必须在 (0, 100] 开度量区间内")
            if stop <= self._position.opening_pct:
                # 阻挡位在当前位置的闭侧：闭到那里就被挡住
                target = max(CLOSED_PCT, stop)
                blocked_at = stop
        while abs(self._position.opening_pct - target) > 1e-9:
            self._position = self._position.approach(target, self._step)
        if blocked_at is not None and self.opening_pct > CLOSED_PCT:
            return self._position.opening_pct, segment_for_opening(self._position.opening_pct)
        return self._position.opening_pct, None

    def _drive(self, target_pct: float) -> float:
        """按步长把行程推到目标位置，返回终点行程。"""

        while abs(self._position.opening_pct - target_pct) > 1e-9:
            self._position = self._position.approach(target_pct, self._step)
        return self._position.opening_pct

    def close_clamp(
        self, mold_id: str, stop_at_opening_pct: float | None = None
    ) -> ClampToken:
        """合模并建立锁模力，达标后签发锁模凭据；卡住则抛联锁错误并指出段。

        ``stop_at_opening_pct`` 给出本模合模途中观测到的机械阻挡位；驱动在该
        位置停住、锁模力不达标时按所处合模段联锁拒绝，绝不允许把行程误判成
        到位。缺省为无阻挡，正常闭到全闭位。
        """

        with self._lock:
            self._state.transition(LiftPhase.CLOSING)
            self._clamp_token = None
            self._bus.publish(
                Event.of("lift", "lift", "close_start", mold_id=mold_id, opening_pct=self.opening_pct)
            )
            self._carried_mold = mold_id
            opening, jammed = self._drive_close(stop_at_opening_pct)
            force = self.clamp_force_kn()
            if jammed is not None or force < self._required_force_kn:
                segment = jammed or segment_for_opening(opening)
                self._bus.publish(
                    Event.of(
                        "lift",
                        "lift",
                        "close_jammed",
                        subject=mold_id,
                        severity="warning",
                        mold_id=mold_id,
                        opening_pct=round(opening, 3),
                        clamp_force_kn=round(force, 2),
                        required_kn=self._required_force_kn,
                        segment=segment.value,
                        segment_label=segment.label,
                    )
                )
                raise InterlockError(
                    f"模具 {mold_id} 合模卡在{segment.label}"
                    f"（行程 {opening:.1f}%，锁模力 {force:.1f}/{self._required_force_kn:.0f} kN），"
                    "未建立锁模力，禁止判定到位",
                    component="lift",
                )
            token = ClampToken(
                mold_id=mold_id,
                clamp_force_kn=force,
                required_kn=self._required_force_kn,
                locked_at=self._clock.now(),
                opening_pct=opening,
                segment=ClampSegment.LOCK.value,
            )
            self._clamp_token = token
            self._state.transition(LiftPhase.CLOSED)
        self._bus.publish(
            Event.of(
                "lift",
                "lift",
                "close_done",
                mold_id=mold_id,
                opening_pct=round(opening, 3),
                clamp_force_kn=round(force, 2),
                required_kn=self._required_force_kn,
                segment=ClampSegment.LOCK.value,
                **{"locked": True, "locked_at": round(token.locked_at, 3)},
            )
        )
        return token

    def retract_clamp(self, mold_id: str) -> float:
        """合模卡住后退回到全开位（排障路径），不签发任何凭据。"""

        with self._lock:
            if self._carried_mold is not None and self._carried_mold != mold_id:
                raise SequenceError(
                    f"退模对象 {mold_id} 与在机模具 {self._carried_mold} 不一致",
                    component="lift",
                )
            if self._state.phase is not LiftPhase.CLOSING:
                raise SequenceError(
                    f"仅合模过程中允许退模，当前相位 {self._state.phase.value}",
                    component="lift",
                )
            opening = self._drive(FULLY_OPEN_PCT)
            self._state.transition(LiftPhase.OPEN)
            self._carried_mold = None
            self._clamp_token = None
        self._bus.publish(
            Event.of("lift", "lift", "close_retracted", mold_id=mold_id, opening_pct=opening)
        )
        return opening

    def open_clamp(self, mold_id: str) -> float:
        """校验同模次锁模凭据后开模到取件位，开模完成注销凭据。"""

        with self._lock:
            if self._carried_mold is not None and self._carried_mold != mold_id:
                raise SequenceError(
                    f"开模对象 {mold_id} 与在机模具 {self._carried_mold} 不一致",
                    component="lift",
                )
            self.require_token_for(mold_id)
            self._state.transition(LiftPhase.OPENING)
            self._bus.publish(
                Event.of("lift", "lift", "open_start", mold_id=mold_id, opening_pct=self.opening_pct)
            )
            opening = self._drive(FULLY_OPEN_PCT)
            self._state.transition(LiftPhase.OPEN)
            self._carried_mold = None
            self._clamp_token = None
        self._bus.publish(
            Event.of("lift", "lift", "open_done", mold_id=mold_id, opening_pct=opening)
        )
        return opening

    def snapshot(self) -> dict:
        with self._lock:
            token = self._clamp_token
            return {
                "phase": self._state.phase.value,
                "history": self._state.history(),
                "position": self._position.as_dict(),
                "segment": segment_for_opening(self.opening_pct).value,
                "clamp_force_kn": round(self.clamp_force_kn(), 2),
                "required_force_kn": self._required_force_kn,
                "locked": self.is_locked(),
                "clamp_token": token.as_dict() if token is not None else None,
                "carried_mold": self._carried_mold,
            }
