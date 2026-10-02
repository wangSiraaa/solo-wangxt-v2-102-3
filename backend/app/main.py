"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from . import receiver_store
from .assemble import bands_view, build_spectrum, to_domain, to_rules, validate_masks
from .config import CORS_ORIGINS, DATABASE_URL
from .db import (Base, CarrierRow, MaskRow, ProtectedReceiverRow, ReportRow,
                 Scenario)
from .schemas import (AnalyzeRequest, MaskOut, PlanRequest, ReceiverIn,
                      ReceiverOut, ReceiverRef, ReportOut, ReportSummary,
                      ScenarioIn, ScenarioOut, ScenarioSummary)
from .seed import seed
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, plan

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="1.1.0",
    description="载波频带冲突检查、掩模尾部泄漏、线性域功率汇总、受保护接收机"
                "窗内总量评估与 OR-Tools 频率规划。"
                "不连接无线电设备，不生成发射指令。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


@app.on_event("startup")
def _startup() -> None:
    Base.metadata.create_all(engine)
    seed(engine)


# ---- 计算核心 ---------------------------------------------------------------

def _run_analyze(req: AnalyzeRequest, s: Session | None, pinned: bool = False) -> tuple[dict, list[dict]]:
    """执行分析并返回 (result, rule_bindings)。

    pinned=True 时按引用中钉住的版本解析（用于复现旧报告），
    否则解析规则库当前版本（已删除规则拒绝参与新分析）。
    """
    validate_masks(req.carriers)
    carriers = [to_domain(c) for c in req.carriers]
    if s is not None and req.receivers:
        try:
            receivers = (receiver_store.resolve_pinned if pinned
                         else receiver_store.resolve_for_new_analysis)(s, req.receivers)
        except receiver_store.ReceiverError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        receivers = []
    result = analyze(carriers, to_rules(req.rules, receivers))
    result["bands"] = bands_view(carriers)
    result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
    bindings = [{"id": r["id"], "version": r["version"], "name": r["name"]}
                for r in result["protected_receivers"]]
    return result, bindings


def _run_plan(req: PlanRequest, s: Session | None, pinned: bool = False) -> tuple[dict, list[dict]]:
    req.validate_band()
    validate_masks(req.carriers)
    carriers = [to_domain(c) for c in req.carriers]
    if s is not None and req.receivers:
        try:
            receivers = (receiver_store.resolve_pinned if pinned
                         else receiver_store.resolve_for_new_analysis)(s, req.receivers)
        except receiver_store.ReceiverError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        receivers = []
    rules = to_rules(req.rules, receivers)
    result = plan(carriers, rules, BandLimits(req.band_low_mhz, req.band_high_mhz),
                  mode=req.mode)
    if result["feasible"]:
        planned = [
            Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                    bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                    polarization=a["polarization"], mask_name=a["mask_name"])
            for a in result["assignments"]
        ]
        result["post_check"] = analyze(planned, rules)
        # 规划后的频段视图与发射谱（用于前端叠加对照）
        result["bands"] = bands_view(planned)
        result["spectrum"] = build_spectrum(planned, 0.05)
    bindings = [{"id": rx.id, "version": rx.version, "name": rx.name}
                for rx in receivers]
    return result, bindings


# ---- 计算接口（无状态，数据由前端提交） -----------------------------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        with Session(engine) as s:
            result, _ = _run_analyze(req, s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        with Session(engine) as s:
            result, _ = _run_plan(req, s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ---- 掩模 ------------------------------------------------------------------

@app.get("/api/masks", response_model=list[MaskOut])
def list_masks() -> list[MaskRow]:
    with Session(engine) as s:
        return list(s.scalars(select(MaskRow).order_by(MaskRow.name)))


# ---- 受保护接收机规则（版本化，修订/删除不重写历史） -----------------------

def _receiver_out(row: ProtectedReceiverRow) -> ReceiverOut:
    return ReceiverOut(
        id=row.id, version=row.version, current_version=row.current_version,
        name=row.name, low_mhz=row.low_mhz, high_mhz=row.high_mhz,
        limit_dbm=row.limit_dbm, polarizations=list(row.polarizations),
        status=row.status, is_current=(row.version == row.current_version),
        created_at=row.created_at.isoformat() if row.created_at else None)


@app.get("/api/receivers", response_model=list[ReceiverOut])
def list_receivers(include_versions: bool = False) -> list[ReceiverOut]:
    with Session(engine) as s:
        rows = (list(s.scalars(select(ProtectedReceiverRow).order_by(
                     ProtectedReceiverRow.id, ProtectedReceiverRow.version)))
                if include_versions else receiver_store.list_current(s))
        return [_receiver_out(r) for r in rows]


@app.get("/api/receivers/{rid}/versions", response_model=list[ReceiverOut])
def receiver_versions(rid: int) -> list[ReceiverOut]:
    with Session(engine) as s:
        try:
            rows = receiver_store.list_versions(s, rid)
        except receiver_store.ReceiverError as e:
            raise HTTPException(404, str(e))
        return [_receiver_out(r) for r in rows]


@app.get("/api/receivers/{rid}", response_model=ReceiverOut)
def get_receiver(rid: int, version: int | None = Query(default=None)) -> ReceiverOut:
    with Session(engine) as s:
        try:
            row = receiver_store.get_version(s, rid, version, allow_deleted=True)
        except receiver_store.ReceiverError as e:
            raise HTTPException(404, str(e))
        return _receiver_out(row)


@app.post("/api/receivers", response_model=ReceiverOut)
def create_receiver_endpoint(req: ReceiverIn) -> ReceiverOut:
    try:
        req.validate_window()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        row = receiver_store.create_receiver(s, req)
        s.commit()
        s.refresh(row)
        return _receiver_out(row)


@app.put("/api/receivers/{rid}", response_model=ReceiverOut)
def revise_receiver_endpoint(rid: int, req: ReceiverIn) -> ReceiverOut:
    try:
        req.validate_window()
        with Session(engine) as s:
            row = receiver_store.revise_receiver(s, rid, req)
            s.commit()
            s.refresh(row)
            return _receiver_out(row)
    except receiver_store.ReceiverError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/receivers/{rid}", response_model=ReceiverOut)
def delete_receiver_endpoint(rid: int) -> ReceiverOut:
    with Session(engine) as s:
        try:
            row = receiver_store.delete_receiver(s, rid)
            s.commit()
            s.refresh(row)
            return _receiver_out(row)
        except receiver_store.ReceiverError as e:
            raise HTTPException(status_code=404, detail=str(e))


# ---- 报告持久化（快照 + 绑定规则版本 + 显式过期） --------------------------

def _report_status(kind: str, result: dict) -> str | None:
    if kind == "analysis":
        return result.get("status")
    if not result.get("feasible"):
        return "infeasible"
    return result.get("post_check", {}).get("status")


def _save_report(s: Session, kind: str, title: str, scenario_id: int | None,
                 result: dict, request_dump: dict, bindings: list[dict]) -> ReportRow:
    stale, reason = receiver_store.staleness(s, bindings)
    row = ReportRow(kind=kind, title=title or f"{kind} 报告",
                    scenario_id=scenario_id, result_json=result,
                    request_json=request_dump, rule_bindings=bindings,
                    stale=stale, stale_reason=reason)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def _report_summary(row: ReportRow, s: Session) -> ReportSummary:
    stale, reason = receiver_store.staleness(s, row.rule_bindings)
    if stale != row.stale:
        row.stale, row.stale_reason = stale, reason
        s.commit()
    return ReportSummary(
        id=row.id, kind=row.kind, title=row.title, scenario_id=row.scenario_id,
        stale=stale, stale_reason=reason, rule_bindings=list(row.rule_bindings),
        status=_report_status(row.kind, row.result_json),
        created_at=row.created_at.isoformat() if row.created_at else None)


def _report_out(row: ReportRow, s: Session, latest: dict | None = None,
                reevaluated: bool = False) -> ReportOut:
    stale, reason = receiver_store.staleness(s, row.rule_bindings)
    if stale != row.stale:
        row.stale, row.stale_reason = stale, reason
        s.commit()
    return ReportOut(
        id=row.id, kind=row.kind, title=row.title, scenario_id=row.scenario_id,
        result=row.result_json, request_snapshot=row.request_json,
        rule_bindings=list(row.rule_bindings), stale=stale, stale_reason=reason,
        reevaluated=reevaluated, latest_result=latest,
        created_at=row.created_at.isoformat() if row.created_at else None)


@app.post("/api/reports/analysis", response_model=ReportOut)
def save_analysis_report(req: AnalyzeRequest, title: str = "",
                         scenario_id: int | None = None) -> ReportOut:
    with Session(engine) as s:
        result, bindings = _run_analyze(req, s)
        row = _save_report(s, "analysis", title, scenario_id, result,
                           req.model_dump(mode="json"), bindings)
        return _report_out(row, s)


@app.post("/api/reports/plan", response_model=ReportOut)
def save_plan_report(req: PlanRequest, title: str = "",
                     scenario_id: int | None = None) -> ReportOut:
    with Session(engine) as s:
        result, bindings = _run_plan(req, s)
        row = _save_report(s, "plan", title, scenario_id, result,
                           req.model_dump(mode="json"), bindings)
        return _report_out(row, s)


@app.get("/api/reports", response_model=list[ReportSummary])
def list_reports() -> list[ReportSummary]:
    with Session(engine) as s:
        rows = list(s.scalars(select(ReportRow).order_by(ReportRow.id.desc())))
        return [_report_summary(r, s) for r in rows]


@app.get("/api/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int, refresh: bool = False) -> ReportOut:
    """取报告：默认原样返回绑定版本的历史结果（可复现）。

    refresh=true 时额外用规则库当前版本跑一遍 latest_result：
    旧结果仍原样保留，二者并列展示“历史 vs 需重新评估”。
    """
    with Session(engine) as s:
        row = s.get(ReportRow, report_id)
        if row is None:
            raise HTTPException(404, "报告不存在")
        latest = None
        if refresh:
            try:
                if row.kind == "analysis":
                    req = AnalyzeRequest.model_validate(row.request_json)
                    # 用当前版本而非绑定版本重跑
                    req.receivers = [ReceiverRef(id=b["id"]) for b in row.rule_bindings]
                    latest, _ = _run_analyze(req, s)
                else:
                    req = PlanRequest.model_validate(row.request_json)
                    req.receivers = [ReceiverRef(id=b["id"]) for b in row.rule_bindings]
                    latest, _ = _run_plan(req, s)
            except HTTPException:
                latest = {"error": "部分绑定规则已删除，无法按当前版本重跑；"
                                   "历史结果仍可按绑定版本复现"}
        return _report_out(row, s, latest=latest, reevaluated=refresh)


@app.post("/api/reports/{report_id}/rerun", response_model=ReportOut)
def rerun_report(report_id: int) -> ReportOut:
    """按报告绑定的规则版本原样重跑（复现历史），不覆盖已存结果。"""
    with Session(engine) as s:
        row = s.get(ReportRow, report_id)
        if row is None:
            raise HTTPException(404, "报告不存在")
        if row.kind == "analysis":
            req = AnalyzeRequest.model_validate(row.request_json)
            result, _ = _run_analyze(req, s, pinned=True)
        else:
            req = PlanRequest.model_validate(row.request_json)
            result, _ = _run_plan(req, s, pinned=True)
        return _report_out(row, s, latest=result, reevaluated=True)


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int) -> dict:
    with Session(engine) as s:
        row = s.get(ReportRow, report_id)
        if row is None:
            raise HTTPException(404, "报告不存在")
        s.delete(row)
        s.commit()
        return {"deleted": report_id}


# ---- 场景持久化 ------------------------------------------------------------

def _carrier_out(c: CarrierRow):
    from .schemas import CarrierOut
    return CarrierOut(id=c.id, name=c.name, center_mhz=c.center_mhz,
                      bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                      polarization=c.polarization, mask_name=c.mask_name)


def _row_to_out(sc: Scenario) -> ScenarioOut:
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_mhz=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        protected_receiver_ids=list(sc.protected_receiver_ids or []),
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
    )


def _validate_receiver_ids(s: Session, ids: list[int]) -> None:
    for rid in ids:
        try:
            receiver_store.get_version(s, rid, None, allow_deleted=False)
        except receiver_store.ReceiverError:
            raise HTTPException(400, f"场景引用了不存在或已删除的受保护接收机规则 {rid}")


@app.get("/api/scenarios", response_model=list[ScenarioSummary])
def list_scenarios() -> list[ScenarioSummary]:
    with Session(engine) as s:
        rows = list(s.scalars(select(Scenario).order_by(Scenario.id)))
        return [ScenarioSummary(id=r.id, name=r.name, description=r.description,
                                carrier_count=len(r.carriers),
                                created_at=r.created_at.isoformat() if r.created_at else None)
                for r in rows]


@app.get("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def get_scenario(scenario_id: int) -> ScenarioOut:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        return _row_to_out(sc)


@app.post("/api/scenarios", response_model=ScenarioOut)
def create_scenario(req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        _validate_receiver_ids(s, req.protected_receiver_ids)
        sc = Scenario(
            name=req.name.strip(), description=req.description,
            band_low_mhz=req.band_low_mhz, band_high_mhz=req.band_high_mhz,
            guard_required_mhz=req.guard_required_mhz,
            leakage_limit_mhz=req.leakage_limit_dbm,
            reuse_policy=dict(req.reuse_policy),
            protected_receiver_ids=list(dict.fromkeys(req.protected_receiver_ids)),
            carriers=[
                CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                           bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                           polarization=c.polarization, mask_name=c.mask_name)
                for i, c in enumerate(req.carriers)
            ],
        )
        s.add(sc)
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int) -> dict:
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        s.delete(sc)
        s.commit()
        return {"deleted": scenario_id}


@app.put("/api/scenarios/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: int, req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        sc = s.get(Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        other = s.scalar(select(Scenario).where(
            Scenario.name == req.name.strip(), Scenario.id != scenario_id))
        if other is not None:
            raise HTTPException(409, f"场景名 {req.name!r} 已存在")
        _validate_receiver_ids(s, req.protected_receiver_ids)
        sc.name = req.name.strip()
        sc.description = req.description
        sc.band_low_mhz = req.band_low_mhz
        sc.band_high_mhz = req.band_high_mhz
        sc.guard_required_mhz = req.guard_required_mhz
        sc.leakage_limit_dbm = req.leakage_limit_dbm
        sc.reuse_policy = dict(req.reuse_policy)
        sc.protected_receiver_ids = list(dict.fromkeys(req.protected_receiver_ids))
        sc.carriers = [
            CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                       bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                       polarization=c.polarization, mask_name=c.mask_name)
            for i, c in enumerate(req.carriers)
        ]
        s.commit()
        s.refresh(sc)
        return _row_to_out(sc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
