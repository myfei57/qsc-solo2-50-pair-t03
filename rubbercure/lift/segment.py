"""合模行程分段。

合模不是“行程到 0% 就算完”：模板要依次走完快速合模、慢速合模、护模低压、
锁模高压四段，真正建立起锁模力的只有最后一段。卡模时按当前行程落点指出
卡在哪一段，现场才能对症处理（快合段多为导轨异物，护模段多为分型面带胶）。

分段按开度量（``opening_pct``，100% 全开、0% 全闭）从开向闭划分：

* 快合段：开度量 (75, 100]
* 慢合段：开度量 (50, 75]
* 护模段：开度量 (25, 50]
* 锁模段：开度量 [0, 25]
"""

from __future__ import annotations

import enum

#: 各段上界（开度量 %），索引与 :class:`ClampSegment` 的闭向顺序一致。
SEGMENT_BOUNDS_PCT: tuple[float, ...] = (25.0, 50.0, 75.0, 100.0)


class ClampSegment(str, enum.Enum):
    """合模由开向闭依次经过的四个行程段。"""

    FAST = "fast_close"
    SLOW = "slow_close"
    GUARD = "guard_low_pressure"
    LOCK = "lock_high_pressure"

    @property
    def label(self) -> str:
        """现场用中文段名。"""

        return _LABELS[self]


_LABELS: dict["ClampSegment", str] = {
    ClampSegment.FAST: "快合段",
    ClampSegment.SLOW: "慢合段",
    ClampSegment.GUARD: "护模低压段",
    ClampSegment.LOCK: "锁模高压段",
}

_ORDERED: tuple[ClampSegment, ...] = (
    ClampSegment.LOCK,
    ClampSegment.GUARD,
    ClampSegment.SLOW,
    ClampSegment.FAST,
)


def segment_for_opening(opening_pct: float) -> ClampSegment:
    """按当前开度量返回所处合模段。"""

    if not 0.0 <= opening_pct <= 100.0:
        raise ValueError(f"行程位置越界: {opening_pct}")
    for segment, bound in zip(_ORDERED, SEGMENT_BOUNDS_PCT):
        if opening_pct <= bound:
            return segment
    raise AssertionError("unreachable")  # pragma: no cover
