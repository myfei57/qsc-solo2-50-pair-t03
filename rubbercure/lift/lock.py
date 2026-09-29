"""锁模到位凭据。

旧逻辑把"行程到 0%"当成合模完成：模具卡在半截时行程开关照样可能给出到位
信号，下游升温就跟着跑，而开模前也没人复核这一模到底锁没锁上。现在合模
完成的唯一判据是锁模力达到工艺下限，达标后由开合模本体签发
:class:`ClampToken`，凭据里同时记录行程开度、实测锁模力与签发时刻。

凭据不随升温、泄压失效——只要模具还处在锁模位、实测锁模力不回落，凭据就
持续有效。开模动作必须出示与在机模具一致的凭据，并当场复测锁模力；拿不出
凭据或复测掉压，说明这一模没有可靠锁过（或锁模力已经泄掉），禁止开模，
先按设备异常排查。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClampToken:
    """锁模到位凭据：合模签发，开模前强制复核。"""

    token_id: str
    mold_id: str
    opening_pct: float
    clamp_force_kn: float
    locked_at: float
    required_force_kn: float

    def belongs_to(self, mold_id: str) -> bool:
        return self.mold_id == mold_id

    def covers(self, mold_id: str, *, live_force_kn: float, opening_pct: float) -> bool:
        """开模前复核：凭据归属本模、行程仍在锁模位、锁模力未回落。"""

        return (
            self.belongs_to(mold_id)
            and opening_pct <= 0.5
            and live_force_kn >= self.required_force_kn
        )

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "mold_id": self.mold_id,
            "opening_pct": round(self.opening_pct, 3),
            "clamp_force_kn": round(self.clamp_force_kn, 2),
            "locked_at": round(self.locked_at, 3),
            "required_force_kn": self.required_force_kn,
        }
