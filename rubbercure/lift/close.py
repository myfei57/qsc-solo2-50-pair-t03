"""合模动作。

合模前必须先排气：模具腔内残存空气会在合模时被压缩成气囊，硫化出来的
制品表面出现缺胶与气泡。排气凭据由蒸汽／排气组件签发，合模动作在动作前
强制校验，缺凭据或凭据过期都拒绝合模。

合模到位与否**只看锁模力**：本体在锁模力达标后签发锁模凭据
（:class:`~rubbercure.lift.token.ClampToken`），行程走到但力没到（模具卡了
半截）会抛联锁错误并指出卡住的合模段，本动作不吞掉这个错误。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..steam.purge import PurgeToken
from .lift import LiftUnit
from .token import ClampToken


def close_mold(
    lift: LiftUnit,
    purge: PurgeToken,
    bus: EventBus,
    mold_id: str,
    *,
    now: float | None = None,
    max_age_s: float | None = None,
) -> dict:
    """校验排气凭据后合模，凭锁模力确认到位并返回含锁模凭据的结果。"""

    if not purge.is_valid():
        raise InterlockError(f"模具 {mold_id} 未完成排气，禁止合模", component="lift")
    if now is not None and max_age_s is not None and purge.is_stale(now, max_age_s):
        raise InterlockError(
            f"模具 {mold_id} 的排气凭据已超过 {max_age_s:.0f}s，需重新排气",
            component="lift",
        )
    token: ClampToken = lift.close_clamp(mold_id)
    result = {
        "mold_id": mold_id,
        "opening_pct": round(token.opening_pct, 3),
        "clamp_force_kn": round(token.clamp_force_kn, 2),
        "required_kn": token.required_kn,
        "locked_at": round(token.locked_at, 3),
        "segment": token.segment,
        "purge_generation": purge.generation,
    }
    bus.publish(
        Event.of(
            "lift",
            "lift",
            "mold_locked",
            subject=mold_id,
            severity="info",
            **result,
        )
    )
    return result
