"""SQLAlchemy 模型：场景、载波、示例频谱掩模、受保护接收机规则（版本化）、分析报告。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    band_low_mhz: Mapped[float] = mapped_column(Float, default=80.0)
    band_high_mhz: Mapped[float] = mapped_column(Float, default=220.0)
    guard_required_mhz: Mapped[float] = mapped_column(Float, default=1.0)
    leakage_limit_dbm: Mapped[float] = mapped_column(Float, default=-45.0)
    # 极化复用规则，如 {"H|V": "unknown", "RHCP|V": "allowed"}
    reuse_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # 本场景分析时绑定的受保护接收机 id（全局规则库；历史报告自身持有版本快照）
    protected_receiver_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    carriers: Mapped[list["CarrierRow"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan",
        order_by="CarrierRow.position")


class CarrierRow(Base):
    __tablename__ = "carriers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    center_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    bandwidth_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    power_dbm: Mapped[float] = mapped_column(Float, nullable=False)
    polarization: Mapped[str] = mapped_column(String(8), nullable=False)
    mask_name: Mapped[str] = mapped_column(String(32), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="carriers")


class MaskRow(Base):
    """示例频谱发射掩模：名称 + 折线点 + 说明（教学示例）。"""
    __tablename__ = "masks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # 一侧（非负偏移）折线点 [[offset_mhz, attenuation_db], ...]
    points: Mapped[list] = mapped_column(JSON, nullable=False)
    span_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class ProtectedReceiverRow(Base):
    """受保护接收机规则。

    修订不覆盖历史：每次修改生成新版本行（version 递增），旧版本原样保留；
    删除也产生一个 status='deleted' 的墓碑新版本。历史报告绑定的 (id, version)
    永远可以取到当时生效的内容，current_version 供新分析解析“当前版本”。
    """
    __tablename__ = "protected_receivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    low_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    high_mhz: Mapped[float] = mapped_column(Float, nullable=False)
    limit_dbm: Mapped[float] = mapped_column(Float, nullable=False)
    # 适用极化，如 ["H"] 或 ["H", "V"]
    polarizations: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/deleted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class ReportRow(Base):
    """分析/规划报告：结果快照与所绑定的规则版本一并固化，历史不可重写。

    result_json 保存完整 analyze/plan 响应；rule_bindings 记录当时用到的每条
    受保护接收机 (id, version)。规则被修订/删除后报告仍可原样复现，
    stale 标记由生成时与查询时比对 current_version 得出（显式过期）。
    """
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # analysis/plan
    scenario_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 输入快照：载波与规则（含接收机版本），用于“按原版本重新运行”复现
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # [{"id": 1, "version": 2, "name": "..."}, ...]
    rule_bindings: Mapped[list] = mapped_column(JSON, default=list)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
