"""合模动作。

合模前必须先排气：模具腔内残存空气会在合模时被压缩成气囊，硫化出来的
制品表面出现缺胶与气泡。排气凭据由蒸汽／排气组件签发，合模动作在动作前
强制校验，缺凭据或凭据过期都拒绝合模。

合模完成不再看行程，而看锁模力：本体走到锁模位并建立起达标锁模力后才
签发锁模到位凭据 :class:`ClampToken`。模具卡顿时本体会按分段报错并告警，
不会再出现"卡在半截却判到位、升温跟着跑"。本动作返回的结果里带锁模凭据
编号与实测锁模力，作为这一模锁到位的凭据。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..steam.purge import PurgeToken
from .lift import LiftUnit
from .lock import ClampToken


def close_mold(
    lift: LiftUnit,
    purge: PurgeToken,
    bus: EventBus,
    mold_id: str,
    *,
    now: float | None = None,
    max_age_s: float | None = None,
    lockup_factor: float = 1.0,
    jam_at_pct: float | None = None,
) -> dict:
    """校验排气凭据后合模，凭锁模力签发到位凭据，返回合模结果。"""

    if not purge.is_valid():
        raise InterlockError(f"模具 {mold_id} 未完成排气，禁止合模", component="lift")
    if now is not None and max_age_s is not None and purge.is_stale(now, max_age_s):
        raise InterlockError(
            f"模具 {mold_id} 的排气凭据已超过 {max_age_s:.0f}s，需重新排气",
            component="lift",
        )
    token: ClampToken = lift.close_clamp(
        mold_id, jam_at_pct=jam_at_pct, lockup_factor=lockup_factor
    )
    result = {
        "mold_id": mold_id,
        "opening_pct": round(token.opening_pct, 3),
        "clamp_force_kn": round(token.clamp_force_kn, 2),
        "required_force_kn": token.required_force_kn,
        "lock_token_id": token.token_id,
        "locked_at": round(token.locked_at, 3),
        "purge_generation": purge.generation,
    }
    bus.publish(
        Event.of("lift", "lift", "mold_clamped", subject=mold_id, severity="info", **result)
    )
    return result
