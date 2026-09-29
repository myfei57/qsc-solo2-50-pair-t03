"""锁模凭据。

合模“到位”不再按行程判定，而按锁模力判定：只有锁模力稳定达到工艺下限，
才签发本凭据。凭据钉在具体模具与具体合模时刻上，随模次保存在开合模本体上；
升温、计时等下游动作凭它放行，开模前必须出示同模次的有效凭据，开模完成后
凭据立即注销。这样“到底锁没锁上”在任何时刻都有据可查，而不是靠行程开关
的一面之词。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClampToken:
    """锁模到位凭据：锁模力达标后签发，开模放行时校验并注销。"""

    mold_id: str
    clamp_force_kn: float
    required_kn: float
    locked_at: float
    opening_pct: float
    segment: str
    valid: bool = True

    def is_valid(self) -> bool:
        """凭据有效要求：未被注销，且签发时锁模力确实达标。"""

        return bool(self.valid) and self.clamp_force_kn >= self.required_kn

    def for_mold(self, mold_id: str) -> bool:
        """凭据是否属于指定模具（防止拿别的模次凭据冒领开模）。"""

        return self.mold_id == mold_id

    def as_dict(self) -> dict:
        return {
            "mold_id": self.mold_id,
            "clamp_force_kn": round(self.clamp_force_kn, 2),
            "required_kn": self.required_kn,
            "locked_at": round(self.locked_at, 3),
            "opening_pct": round(self.opening_pct, 3),
            "segment": self.segment,
            "valid": self.is_valid(),
        }
