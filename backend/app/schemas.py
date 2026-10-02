"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

POLARIZATIONS = ("H", "V", "LHCP", "RHCP")
REUSE_VALUES = ("forbidden", "allowed", "unknown")
RECEIVER_STATUSES = ("active", "deleted")


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


class ReceiverRef(BaseModel):
    """分析/规划请求对受保护接收机规则的引用。

    不指定 version 时解析规则库中的当前版本；显式给定 version 时按该历史
    版本运行（用于复现旧报告，即使规则之后被修订或删除）。
    """
    id: int
    version: Optional[int] = None


class RulesIn(BaseModel):
    guard_required_mhz: float = Field(default=1.0, ge=0)
    leakage_limit_dbm: float = -45.0
    # key 形如 "H|V"（极化名按字母排序后拼接），value forbidden/allowed/unknown
    reuse_policy: dict[str, Literal["forbidden", "allowed", "unknown"]] = Field(default_factory=dict)


class ReceiverIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    low_mhz: float
    high_mhz: float
    limit_dbm: float
    polarizations: list[Literal["H", "V", "LHCP", "RHCP"]] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def defuzz_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("接收机名称不能为空")
        return v

    @field_validator("polarizations")
    @classmethod
    def dedup_pols(cls, v: list[str]) -> list[str]:
        out = sorted(set(v))
        if not out:
            raise ValueError("至少指定一个适用极化")
        return out

    def validate_window(self) -> None:
        if self.high_mhz <= self.low_mhz:
            raise ValueError("high_mhz 必须大于 low_mhz")


class AnalyzeRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    # 受保护接收机引用（保存在规则库中、带版本）；也可走 /receivers/evaluate 只试算
    receivers: list[ReceiverRef] = Field(default_factory=list)
    # 绘图网格步长 (MHz)
    plot_grid_mhz: float = Field(default=0.05, gt=0, le=1.0)


class PlanRequest(BaseModel):
    carriers: list[CarrierIn] = Field(min_length=1)
    rules: RulesIn = RulesIn()
    receivers: list[ReceiverRef] = Field(default_factory=list)
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
    protected_receiver_ids: list[int] = Field(default_factory=list)
    carriers: list[CarrierIn] = Field(default_factory=list)


class ScenarioSummary(BaseModel):
    id: int
    name: str
    description: str
    carrier_count: int
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
    protected_receiver_ids: list[int] = []
    carriers: list[CarrierOut]


class MaskOut(BaseModel):
    name: str
    points: list[list[float]]
    span_mhz: float
    description: str


class ReceiverOut(BaseModel):
    """规则库中的一条受保护接收机版本。"""
    id: int
    version: int
    current_version: int
    name: str
    low_mhz: float
    high_mhz: float
    limit_dbm: float
    polarizations: list[str]
    status: str
    is_current: bool
    created_at: Optional[str] = None


class ReportSummary(BaseModel):
    id: int
    kind: str
    title: str
    scenario_id: Optional[int] = None
    stale: bool
    stale_reason: str
    rule_bindings: list[dict]
    status: Optional[str] = None
    created_at: Optional[str] = None


class ReportOut(BaseModel):
    id: int
    kind: str
    title: str
    scenario_id: Optional[int] = None
    result: dict
    request_snapshot: dict
    rule_bindings: list[dict]
    stale: bool
    stale_reason: str
    reevaluated: bool = False
    latest_result: Optional[dict] = None
    created_at: Optional[str] = None
