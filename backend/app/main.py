"""FastAPI 频谱工作台（离线简化模型）。

不连接无线电设备、不生成发射指令；只对录入的载波数据做计算与可视化。
"""
from __future__ import annotations

import copy

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .assemble import (bands_view, build_spectrum, protected_windows_view,
                       receiver_to_domain, to_domain, to_rules, validate_masks)
from .config import CORS_ORIGINS, DATABASE_URL
from .db import (AnalysisReportRow, Base, CarrierRow, MaskRow,
                 PlanRecordRow, ProtectedReceiverRow, Scenario,
                 ScenarioReceiverLink)
from .schemas import (AnalyzeRequest, MaskOut, PlanRequest,
                      ProtectedReceiverIn, ProtectedReceiverOut,
                      ProtectedReceiverRevisionIn, ScenarioIn, ScenarioOut,
                      ScenarioSummary)
from .seed import seed
from .services.analysis import Carrier, analyze
from .services.planner import BandLimits, plan

app = FastAPI(
    title="频谱工作台 API（离线教学模型）",
    version="1.0.0",
    description="载波频带冲突检查、掩模泄漏、受保护接收机、线性域功率汇总与 OR-Tools 规划。"
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


# ---- 受保护接收机规则版本 --------------------------------------------------

def _validate_receiver_window(low: float, high: float) -> None:
    if high <= low:
        raise ValueError("window_high_mhz 必须大于 window_low_mhz")


def _receiver_out(row: ProtectedReceiverRow) -> ProtectedReceiverOut:
    return ProtectedReceiverOut(
        receiver_key=row.receiver_key, version=row.version, name=row.name,
        window_low_mhz=row.window_low_mhz, window_high_mhz=row.window_high_mhz,
        power_limit_dbm=row.power_limit_dbm, polarizations=list(row.polarizations),
        description=row.description or "", is_current=row.is_current,
        is_deleted=row.is_deleted,
        created_at=row.created_at.isoformat() if row.created_at else None)


def _current_receiver_rows(s: Session, keys: list[str]) -> list[ProtectedReceiverRow]:
    unique = list(dict.fromkeys(keys))
    rows = list(s.scalars(select(ProtectedReceiverRow).where(
        ProtectedReceiverRow.receiver_key.in_(unique),
        ProtectedReceiverRow.is_current.is_(True))))
    by_key = {r.receiver_key: r for r in rows}
    missing = [k for k in unique if k not in by_key]
    if missing:
        raise ValueError(f"受保护接收机规则不存在、已删除或无当前版本: {missing}")
    return [by_key[k] for k in unique]


def _current_receiver_map(s: Session) -> dict[str, ProtectedReceiverRow]:
    rows = s.scalars(select(ProtectedReceiverRow).where(
        ProtectedReceiverRow.is_current.is_(True)))
    return {r.receiver_key: r for r in rows}


def _rule_binding(rules_req, receiver_rows: list[ProtectedReceiverRow]) -> dict:
    return {
        "guard_required_mhz": rules_req.guard_required_mhz,
        "leakage_limit_dbm": rules_req.leakage_limit_dbm,
        "reuse_policy": dict(rules_req.reuse_policy),
        "protected_receivers": [{
            "receiver_key": r.receiver_key,
            "version": r.version,
            "name": r.name,
            "window_low_mhz": r.window_low_mhz,
            "window_high_mhz": r.window_high_mhz,
            "power_limit_dbm": r.power_limit_dbm,
            "polarizations": list(r.polarizations),
            "description": r.description or "",
        } for r in receiver_rows],
    }


def _rule_freshness(s: Session, binding: dict | None) -> dict:
    current = _current_receiver_map(s)
    entries = []
    stale = False
    for snap in (binding or {}).get("protected_receivers", []):
        cur = current.get(snap["receiver_key"])
        if cur is None:
            state = "deleted"
            stale = True
            message = "接收机规则已删除；历史结果保留原版本，需要重新评估"
        elif cur.version != snap["version"]:
            state = "superseded"
            stale = True
            message = (f"接收机规则已修订（v{snap['version']} → v{cur.version}）；"
                       "历史结果仍按原版本复现，需要重新评估")
        else:
            state = "current"
            message = "当前版本"
        entries.append({
            "receiver_key": snap["receiver_key"],
            "bound_version": snap["version"],
            "current_version": None if cur is None else cur.version,
            "state": state,
            "message": message,
        })
    return {"stale": stale, "protected_receivers": entries}


@app.get("/api/protected-receivers", response_model=list[ProtectedReceiverOut])
def list_current_receivers() -> list[ProtectedReceiverOut]:
    with Session(engine) as s:
        rows = list(s.scalars(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.is_current.is_(True),
            ProtectedReceiverRow.is_deleted.is_(False)
        ).order_by(ProtectedReceiverRow.receiver_key)))
        return [_receiver_out(r) for r in rows]


@app.get("/api/protected-receivers/{receiver_key}/versions",
         response_model=list[ProtectedReceiverOut])
def list_receiver_versions(receiver_key: str) -> list[ProtectedReceiverOut]:
    with Session(engine) as s:
        rows = list(s.scalars(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.receiver_key == receiver_key
        ).order_by(ProtectedReceiverRow.version)))
        if not rows:
            raise HTTPException(404, "受保护接收机规则不存在")
        return [_receiver_out(r) for r in rows]


@app.post("/api/protected-receivers", response_model=ProtectedReceiverOut)
def create_receiver(req: ProtectedReceiverIn) -> ProtectedReceiverOut:
    try:
        _validate_receiver_window(req.window_low_mhz, req.window_high_mhz)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        existing = s.scalar(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.receiver_key == req.receiver_key))
        if existing is not None:
            raise HTTPException(409, f"受保护接收机身份 {req.receiver_key!r} 已存在；请使用修订接口")
        row = ProtectedReceiverRow(
            receiver_key=req.receiver_key, version=1, name=req.name.strip(),
            window_low_mhz=req.window_low_mhz, window_high_mhz=req.window_high_mhz,
            power_limit_dbm=req.power_limit_dbm, polarizations=req.polarizations,
            description=req.description, is_current=True, is_deleted=False)
        s.add(row)
        s.commit()
        s.refresh(row)
        return _receiver_out(row)


@app.post("/api/protected-receivers/{receiver_key}/revisions",
          response_model=ProtectedReceiverOut)
def revise_receiver(receiver_key: str, req: ProtectedReceiverRevisionIn) -> ProtectedReceiverOut:
    try:
        _validate_receiver_window(req.window_low_mhz, req.window_high_mhz)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with Session(engine) as s:
        current = s.scalar(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.receiver_key == receiver_key,
            ProtectedReceiverRow.is_current.is_(True)))
        if current is None:
            raise HTTPException(404, "受保护接收机当前版本不存在，不能修订")
        current.is_current = False
        row = ProtectedReceiverRow(
            receiver_key=receiver_key, version=current.version + 1,
            name=req.name.strip(), window_low_mhz=req.window_low_mhz,
            window_high_mhz=req.window_high_mhz, power_limit_dbm=req.power_limit_dbm,
            polarizations=req.polarizations, description=req.description,
            is_current=True, is_deleted=False)
        s.add(row)
        s.commit()
        s.refresh(row)
        return _receiver_out(row)


@app.delete("/api/protected-receivers/{receiver_key}")
def delete_receiver(receiver_key: str) -> dict:
    with Session(engine) as s:
        current = s.scalar(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.receiver_key == receiver_key,
            ProtectedReceiverRow.is_current.is_(True)))
        if current is None:
            raise HTTPException(404, "受保护接收机当前版本不存在")
        current.is_current = False
        current.is_deleted = True
        s.commit()
        return {"deleted": receiver_key,
                "message": "身份已软删除；历史分析和计划仍保留绑定版本，不会重写"}


# ---- 计算接口（无状态提交，但每次计算生成不可变历史记录） -----------------

@app.post("/api/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    try:
        validate_masks(req.carriers)
        with Session(engine) as s:
            receiver_rows = _current_receiver_rows(s, req.protected_receiver_keys)
            receivers = [receiver_to_domain(r) for r in receiver_rows]
            rules = to_rules(req.rules, receivers)
            carriers = [to_domain(c) for c in req.carriers]
            result = analyze(carriers, rules)
            result["input_carriers"] = [c.model_dump() for c in req.carriers]
            result["rule_binding"] = _rule_binding(req.rules, receiver_rows)
            result["protected_windows"] = protected_windows_view(receivers)
            result["bands"] = bands_view(carriers)
            result["spectrum"] = build_spectrum(carriers, req.plot_grid_mhz)
            stored = copy.deepcopy(result)
            report = AnalysisReportRow(scenario_id=req.scenario_id, result=stored)
            s.add(report)
            s.commit()
            report_id = report.id
            freshness = _rule_freshness(s, stored.get("rule_binding"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["id"] = report_id
    result["rule_freshness"] = freshness
    return result


@app.post("/api/plan")
def plan_endpoint(req: PlanRequest) -> dict:
    try:
        req.validate_band()
        validate_masks(req.carriers)
        with Session(engine) as s:
            receiver_rows = _current_receiver_rows(s, req.protected_receiver_keys)
            receivers = [receiver_to_domain(r) for r in receiver_rows]
            rules = to_rules(req.rules, receivers)
            carriers = [to_domain(c) for c in req.carriers]
            result = plan(carriers, rules,
                          BandLimits(req.band_low_mhz, req.band_high_mhz),
                          mode=req.mode)
            result["rule_binding"] = _rule_binding(req.rules, receiver_rows)
            result["input_carriers"] = [c.model_dump() for c in req.carriers]
            result["band_request_mhz"] = [req.band_low_mhz, req.band_high_mhz]
            result["protected_windows"] = protected_windows_view(receivers)
            if result["feasible"]:
                planned = [
                    Carrier(id=None, name=a["name"], center_mhz=a["center_mhz"],
                            bandwidth_mhz=a["bandwidth_mhz"], power_dbm=a["power_dbm"],
                            polarization=a["polarization"], mask_name=a["mask_name"])
                    for a in result["assignments"]
                ]
                result["post_check"] = analyze(planned, rules)
                result["bands"] = bands_view(planned)
                result["spectrum"] = build_spectrum(planned, 0.05)
            stored = copy.deepcopy(result)
            record = PlanRecordRow(scenario_id=req.scenario_id, result=stored)
            s.add(record)
            s.commit()
            record_id = record.id
            freshness = _rule_freshness(s, stored.get("rule_binding"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["id"] = record_id
    result["rule_freshness"] = freshness
    return result


@app.get("/api/analysis/{report_id}")
def get_analysis_report(report_id: int) -> dict:
    with Session(engine) as s:
        row = s.get(AnalysisReportRow, report_id)
        if row is None:
            raise HTTPException(404, "分析报告不存在")
        result = copy.deepcopy(row.result)
        result["id"] = row.id
        result["created_at"] = row.created_at.isoformat() if row.created_at else None
        result["rule_freshness"] = _rule_freshness(s, result.get("rule_binding"))
        return result


@app.get("/api/plans/{plan_id}")
def get_plan_record(plan_id: int) -> dict:
    with Session(engine) as s:
        row = s.get(PlanRecordRow, plan_id)
        if row is None:
            raise HTTPException(404, "规划记录不存在")
        result = copy.deepcopy(row.result)
        result["id"] = row.id
        result["created_at"] = row.created_at.isoformat() if row.created_at else None
        result["rule_freshness"] = _rule_freshness(s, result.get("rule_binding"))
        return result


def _record_summary(kind: str, row, s: Session) -> dict:
    result = row.result or {}
    binding = result.get("rule_binding")
    freshness = _rule_freshness(s, binding)
    post = result.get("post_check", result) if kind == "plan" else result
    return {
        "id": row.id,
        "kind": kind,
        "scenario_id": row.scenario_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "status": post.get("status"),
        "feasible": result.get("feasible") if kind == "plan" else None,
        "counts": post.get("counts"),
        "stale": freshness["stale"],
        "rule_freshness": freshness,
    }


@app.get("/api/scenarios/{scenario_id}/analyses")
def list_scenario_analyses(scenario_id: int,
                           limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    with Session(engine) as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        rows = list(s.scalars(select(AnalysisReportRow).where(
            AnalysisReportRow.scenario_id == scenario_id
        ).order_by(AnalysisReportRow.id.desc()).limit(limit)))
        return [_record_summary("analysis", r, s) for r in rows]


@app.get("/api/scenarios/{scenario_id}/plans")
def list_scenario_plans(scenario_id: int,
                        limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    with Session(engine) as s:
        if s.get(Scenario, scenario_id) is None:
            raise HTTPException(404, "场景不存在")
        rows = list(s.scalars(select(PlanRecordRow).where(
            PlanRecordRow.scenario_id == scenario_id
        ).order_by(PlanRecordRow.id.desc()).limit(limit)))
        return [_record_summary("plan", r, s) for r in rows]


# ---- 掩模 ------------------------------------------------------------------

@app.get("/api/masks", response_model=list[MaskOut])
def list_masks() -> list[MaskRow]:
    with Session(engine) as s:
        return list(s.scalars(select(MaskRow).order_by(MaskRow.name)))


# ---- 场景持久化 ------------------------------------------------------------

def _carrier_out(c: CarrierRow):
    from .schemas import CarrierOut
    return CarrierOut(id=c.id, name=c.name, center_mhz=c.center_mhz,
                      bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                      polarization=c.polarization, mask_name=c.mask_name)


def _row_to_out(sc: Scenario, current_rows: dict[str, ProtectedReceiverRow] | None = None) -> ScenarioOut:
    keys = [link.receiver_key for link in sc.receiver_links]
    rows = []
    missing = []
    if current_rows is not None:
        for k in keys:
            if k in current_rows:
                rows.append(current_rows[k])
            else:
                missing.append(k)
    return ScenarioOut(
        id=sc.id, name=sc.name, description=sc.description,
        band_low_mhz=sc.band_low_mhz, band_high_mhz=sc.band_high_mhz,
        guard_required_mhz=sc.guard_required_mhz,
        leakage_limit_dbm=sc.leakage_limit_dbm,
        reuse_policy=sc.reuse_policy or {},
        protected_receiver_keys=keys,
        protected_receivers=[_receiver_out(r) for r in rows],
        missing_protected_receiver_keys=missing,
        carriers=[_carrier_out(c) for c in sorted(sc.carriers, key=lambda c: c.position)],
    )


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
        return _row_to_out(sc, _current_receiver_map(s))


@app.post("/api/scenarios", response_model=ScenarioOut)
def create_scenario(req: ScenarioIn) -> ScenarioOut:
    try:
        validate_masks(req.carriers)
        with Session(engine) as s:
            _current_receiver_rows(s, req.protected_receiver_keys)
            if s.scalar(select(Scenario).where(Scenario.name == req.name)) is not None:
                raise LookupError(f"场景名 {req.name!r} 已存在")
            sc = Scenario(
                name=req.name.strip(), description=req.description,
                band_low_mhz=req.band_low_mhz, band_high_mhz=req.band_high_mhz,
                guard_required_mhz=req.guard_required_mhz,
                leakage_limit_dbm=req.leakage_limit_dbm,
                reuse_policy=dict(req.reuse_policy),
                receiver_links=[ScenarioReceiverLink(receiver_key=k)
                                for k in dict.fromkeys(req.protected_receiver_keys)],
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
            return _row_to_out(sc, _current_receiver_map(s))
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        with Session(engine) as s:
            _current_receiver_rows(s, req.protected_receiver_keys)
            sc = s.get(Scenario, scenario_id)
            if sc is None:
                raise LookupError("场景不存在")
            other = s.scalar(select(Scenario).where(
                Scenario.name == req.name.strip(), Scenario.id != scenario_id))
            if other is not None:
                raise RuntimeError(f"场景名 {req.name!r} 已存在")
            sc.name = req.name.strip()
            sc.description = req.description
            sc.band_low_mhz = req.band_low_mhz
            sc.band_high_mhz = req.band_high_mhz
            sc.guard_required_mhz = req.guard_required_mhz
            sc.leakage_limit_dbm = req.leakage_limit_dbm
            sc.reuse_policy = dict(req.reuse_policy)
            sc.receiver_links = [ScenarioReceiverLink(receiver_key=k)
                                 for k in dict.fromkeys(req.protected_receiver_keys)]
            sc.carriers = [
                CarrierRow(position=i, name=c.name, center_mhz=c.center_mhz,
                           bandwidth_mhz=c.bandwidth_mhz, power_dbm=c.power_dbm,
                           polarization=c.polarization, mask_name=c.mask_name)
                for i, c in enumerate(req.carriers)
            ]
            s.commit()
            s.refresh(sc)
            return _row_to_out(sc, _current_receiver_map(s))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": "offline simplified — no radio, no transmit commands"}
