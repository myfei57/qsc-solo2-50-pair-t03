"""开模动作。

硫化结束必须先把蒸汽泄压，再开模取件：带着残余压力开模，制品会被腔内
压力顶裂，模具分型面也可能被冲伤。泄压凭据由蒸汽组件在压力退到安全值
之后签发，开模动作在动作前强制校验。

开模前还必须出示合模时签发的锁模到位凭据 :class:`ClampToken`，并当场复测
行程与锁模力：拿不出凭据说明这一模从没按锁模力确认过到位，凭据归属的模具
与在机模具不一致、或复测时锁模力已经回落，都说明这一模的状态不可信，禁止
开模，先排查锁模机构。凭据编号记入开模事件，做到"开模有据、一模一据"。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..steam.vent import VentToken
from .lift import LiftUnit
from .lock import ClampToken


def open_mold(
    lift: LiftUnit,
    vent: VentToken,
    bus: EventBus,
    mold_id: str,
    lock: ClampToken | None = None,
) -> dict:
    """校验泄压凭据与锁模到位凭据后开模，返回开模结果。"""

    if not vent.is_safe():
        raise InterlockError(
            f"模具 {mold_id} 蒸汽未泄压（残余 {vent.residual_kpa:.3f} kPa），禁止开模",
            component="lift",
        )
    token = lock if lock is not None else lift.lock_token()
    if token is None:
        raise InterlockError(
            f"模具 {mold_id} 没有锁模到位凭据，无法确认这一模曾锁到位，禁止开模",
            component="lift",
        )
    live_force = lift.clamp_force_kn()
    if not token.covers(
        mold_id, live_force_kn=live_force, opening_pct=lift.opening_pct
    ):
        if not token.belongs_to(mold_id):
            reason = f"凭据属于模具 {token.mold_id}，与在机模具 {mold_id} 不一致"
        else:
            reason = (
                f"开模前复测行程 {lift.opening_pct:.1f}%、"
                f"锁模力 {live_force:.1f} kN（要求 ≥ {token.required_force_kn:.0f} kN），"
                "锁模状态已不成立"
            )
        raise InterlockError(f"模具 {mold_id} 锁模凭据复核失败：{reason}", component="lift")
    opening = lift.open_clamp(mold_id)
    result = {
        "mold_id": mold_id,
        "opening_pct": round(opening, 3),
        "residual_kpa": round(vent.residual_kpa, 4),
        "lock_token_id": token.token_id,
        "locked_force_kn": round(token.clamp_force_kn, 2),
    }
    bus.publish(
        Event.of(
            "lift",
            "lift",
            "mold_opened",
            subject=mold_id,
            severity="notice",
            **result,
        )
    )
    return result
