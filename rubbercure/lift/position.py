"""合模行程位置与分段。

开合模用一个 0–100% 的行程量表示：0% 表示模具完全闭合、达到锁模位，100%
表示完全打开、可以取件。合模与开模的方向相反，位置对象本身只描述行程，
不判断顺序；顺序约束由合模／开模动作在动作前显式检查。

合模全程按开度分成四段：快速段、低压护模段、高压锁模段、锁模位。以前
只看"行程有没有到 0%"，模具卡在半截也会被判成到位；现在到位凭据按锁模力
签发，但分段仍然保留——卡顿时必须报得出到底卡在哪一段，维修才能按段排查
导向、护模压力与锁模机构。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

FULLY_OPEN_PCT = 100.0
CLOSED_PCT = 0.0

# 各合模分段的开度边界（按 opening_pct 自大开向小闭划分）。
LOW_PRESS_START_PCT = 40.0
HIGH_PRESS_START_PCT = 10.0


class TravelSegment(str, enum.Enum):
    """合模行程分段。"""

    FAST = "fast"
    LOW_PRESS = "low_press"
    HIGH_PRESS = "high_press"
    LOCKED = "locked"

    @property
    def label(self) -> str:
        """现场用语义化段名。"""

        return {
            TravelSegment.FAST: "快速合模段",
            TravelSegment.LOW_PRESS: "低压护模段",
            TravelSegment.HIGH_PRESS: "高压锁模段",
            TravelSegment.LOCKED: "锁模位",
        }[self]


def segment_for_opening(opening_pct: float) -> TravelSegment:
    """按当前开度判断处于合模全程的哪一段。"""

    if opening_pct <= CLOSED_PCT:
        return TravelSegment.LOCKED
    if opening_pct <= HIGH_PRESS_START_PCT:
        return TravelSegment.HIGH_PRESS
    if opening_pct <= LOW_PRESS_START_PCT:
        return TravelSegment.LOW_PRESS
    return TravelSegment.FAST


@dataclass(frozen=True)
class ClampPosition:
    """一个行程位置。"""

    opening_pct: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.opening_pct <= 100.0:
            raise ValueError(f"行程位置越界: {self.opening_pct}")

    def segment(self) -> TravelSegment:
        return segment_for_opening(self.opening_pct)

    def is_closed(self, tolerance_pct: float = 0.5) -> bool:
        return self.opening_pct <= tolerance_pct

    def is_open(self, tolerance_pct: float = 0.5) -> bool:
        return self.opening_pct >= FULLY_OPEN_PCT - tolerance_pct

    def approach(self, target_pct: float, step_pct: float) -> "ClampPosition":
        """朝目标位置移动一步，不越过目标。"""

        if step_pct <= 0:
            raise ValueError("行程步长必须为正")
        target = max(CLOSED_PCT, min(FULLY_OPEN_PCT, float(target_pct)))
        if abs(target - self.opening_pct) <= step_pct:
            return ClampPosition(target)
        direction = 1.0 if target > self.opening_pct else -1.0
        return ClampPosition(self.opening_pct + direction * step_pct)

    def as_dict(self) -> dict:
        return {
            "opening_pct": round(self.opening_pct, 3),
            "segment": self.segment().value,
            "closed": self.is_closed(),
            "open": self.is_open(),
        }
