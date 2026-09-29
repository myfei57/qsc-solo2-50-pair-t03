"""开模动作。

硫化结束开模有两道硬前置，缺一不可：

1. 蒸汽已泄压、模具内无残余压力——泄压凭据由蒸汽组件在压力退到安全值之后
   签发，带压开模会把制品顶裂、冲伤分型面；
2. 本模确实锁上过——开合模本体必须持有属于该模具的**有效锁模凭据**
   （合模时锁模力达标才签发）。没有凭据说明这一模从来没被确认锁到位，
   可能是卡模后被行程开关误判，此时禁止开模，先排查合模段。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..steam.vent import VentToken
from .lift import LiftUnit


def open_mold(lift: LiftUnit, vent: VentToken, bus: EventBus, mold_id: str) -> dict:
    """校验泄压凭据与锁模凭据后开模，返回开模结果。"""

    if not vent.is_safe():
        raise InterlockError(
            f"模具 {mold_id} 蒸汽未泄压（残余 {vent.residual_kpa:.3f} kPa），禁止开模",
            component="lift",
        )
    lock = lift.require_token_for(mold_id)
    opening = lift.open_clamp(mold_id)
    result = {
        "mold_id": mold_id,
        "opening_pct": round(opening, 3),
        "residual_kpa": round(vent.residual_kpa, 4),
        "locked_at": round(lock.locked_at, 3),
        "clamp_force_kn": round(lock.clamp_force_kn, 2),
    }
    bus.publish(
        Event.of("lift", "lift", "mold_opened", subject=mold_id, severity="notice", **result)
    )
    return result
