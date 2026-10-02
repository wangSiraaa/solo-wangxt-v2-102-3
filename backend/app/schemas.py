"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

POLARIZATIONS = ("H", "V", "LHCP", "RHCP")
REUSE_VALUES = ("forbidden", "allowed", "unknown")


class CarrierIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    center_mhz: float = Field(gt=0)
    bandwidth_mhz: float = Field(gt=0)
    power_dbm: float
    polarization: Literal["H", "V", "LHCP", "RHCP"]
    mask_name: str = "strict"

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("载波名不能为空")
        return v


class RulesIn(BaseModel):
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    # key 形如 "H|V"（极化名按字母排序后拼接），value forbidden/allowed/unknown
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)


class ProtectedReceiverIn(BaseModel):
    """创建受保护接收机身份时的首个版本。"""
    receiver_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field(min_length=1, max_length=128)
    window_low_mhz: float
    window_high_mhz: float
    power_limit_dbm: float
    polarizations: list[Literal["H", "V", "LHCP", "RHCP"]] = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("polarizations")
    @classmethod
    def unique_pols(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(v))
        if len(out) != len(v):
            raise ValueError("适用极化不能重复")
        return out


class ProtectedReceiverRevisionIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    window_low_mhz: float
    window_high_mhz: float
    power_limit_dbm: float
    polarizations: list[Literal["H", "V", "LHCP", "RHCP"]] = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("polarizations")
    @classmethod
    def unique_pols(cls, v: list[str]) -> list[str]:
        out = list(dict.fromkeys(v))
        if len(out) != len(v):
            raise ValueError("适用极化不能重复")
        return out


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 服务端按 key 解析当前版本；历史结果保存解析后的完整快照。
    protected_receiver_keys: list[str] = Field(default_factory=list)
    scenario_id: Optional[int] = None
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    protected_receiver_keys: list[str] = Field(default_factory=list)
    scenario_id: Optional[int] = None
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    mode: Literal["guard_only", "mask_aware"] = "guard_only"

    def validate_band(self) -> None:
        if self.band_high_mhz <= self.band_low_mhz:
            raise ValueError("band_high_mhz 必须大于 band_low_mhz")


class CarrierOut(BaseModel):
    id: Optional[int] = None
    name: str
    center_mhz: float
    bandwidth_mhz: float
    power_dbm: float
    polarization: str
    mask_name: str


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    band_low_mhz: float = 80.0
    band_high_mhz: float = 220.0
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)
    protected_receiver_keys: list[str] = Field(default_factory=list)
    carriers: list[CarrierIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
    created_at: Optional[str] = None


class ProtectedReceiverOut(BaseModel):
    receiver_key: str
    version: int
    name: str
    window_low_mhz: float
    window_high_mhz: float
    power_limit_dbm: float
    polarizations: list[str]
    description: str
    is_current: bool
    is_deleted: bool
    created_at: Optional[str] = None


class ScenarioOut(BaseModel):
    id: int
    name: str
    description: str
    band_low_mhz: float
    band_high_mhz: float
    guard_required_mhz: float
    leakage_limit_dbm: float
    reuse_policy: dict[str, str]
    protected_receiver_keys: list[str]
    protected_receivers: list[ProtectedReceiverOut]
    missing_protected_receiver_keys: list[str] = []
    carriers: list[CarrierOut]


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str
