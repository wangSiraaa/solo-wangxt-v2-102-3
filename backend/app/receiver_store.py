"""受保护接收机规则库：版本化存取（修订/删除均不重写历史）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import ProtectedReceiverRow
from .schemas import ReceiverIn
from .services.analysis import ProtectedReceiver


class ReceiverError(LookupError):
    """规则不存在 / 已删除 / 版本缺失等，供 API 层转成 HTTP 错误。"""


def row_to_domain(row: ProtectedReceiverRow) -> ProtectedReceiver:
    return ProtectedReceiver(
        id=row.id, name=row.name, low_mhz=row.low_mhz, high_mhz=row.high_mhz,
        limit_dbm=row.limit_dbm, polarizations=tuple(row.polarizations),
        version=row.version, status=row.status)


def _current_version_number(s: Session, rid: int) -> int | None:
    row = s.scalars(
        select(ProtectedReceiverRow)
        .where(ProtectedReceiverRow.id == rid)
        .order_by(ProtectedReceiverRow.version.desc())
    ).first()
    return None if row is None else row.current_version


def get_version(s: Session, rid: int, version: int | None = None,
                allow_deleted: bool = True) -> ProtectedReceiverRow:
    """取指定版本；version=None 取当前版本。

    allow_deleted=False 时当前版本为 deleted 墓碑会报错（新分析不许引用已删规则；
    复现旧报告时显式绑定历史版本，始终允许）。
    """
    cur = _current_version_number(s, rid)
    if cur is None:
        raise ReceiverError(f"受保护接收机规则 {rid} 不存在")
    want = version if version is not None else cur
    row = s.scalar(select(ProtectedReceiverRow).where(
        ProtectedReceiverRow.id == rid, ProtectedReceiverRow.version == want))
    if row is None:
        raise ReceiverError(f"受保护接收机规则 {rid} 的版本 {want} 不存在")
    if not allow_deleted and row.status == "deleted" and version is None:
        raise ReceiverError(f"受保护接收机规则 {rid!r} 已删除，请改用其他规则")
    return row


def resolve_for_new_analysis(s: Session, refs) -> list[ProtectedReceiver]:
    """新分析/规划：未钉版本的引用解析当前版本，已删除规则直接报错。"""
    out: list[ProtectedReceiver] = []
    for ref in refs:
        row = get_version(s, ref.id, ref.version, allow_deleted=False)
        out.append(row_to_domain(row))
    return out


def resolve_pinned(s: Session, refs) -> list[ProtectedReceiver]:
    """按绑定版本复现：历史版本即使在后来被删除也照常取出。"""
    return [row_to_domain(get_version(s, ref.id, ref.version, allow_deleted=True))
            for ref in refs]


def list_current(s: Session) -> list[ProtectedReceiverRow]:
    rows = list(s.scalars(select(ProtectedReceiverRow).order_by(
        ProtectedReceiverRow.id, ProtectedReceiverRow.version)))
    return [r for r in rows if r.version == r.current_version]


def list_versions(s: Session, rid: int) -> list[ProtectedReceiverRow]:
    if _current_version_number(s, rid) is None:
        raise ReceiverError(f"受保护接收机规则 {rid} 不存在")
    return list(s.scalars(
        select(ProtectedReceiverRow)
        .where(ProtectedReceiverRow.id == rid)
        .order_by(ProtectedReceiverRow.version)))


def _append_version(s: Session, rid: int | None, data: ReceiverIn,
                    status: str) -> ProtectedReceiverRow:
    if rid is None:
        # 新规则：先占一个 id
        rid = (s.scalar(select(ProtectedReceiverRow.id)
                        .order_by(ProtectedReceiverRow.id.desc())) or 0) + 1
        version = 1
    else:
        cur_no = _current_version_number(s, rid)
        if cur_no is None:
            raise ReceiverError(f"受保护接收机规则 {rid} 不存在")
        version = cur_no + 1
    row = ProtectedReceiverRow(
        id=rid, version=version, current_version=version,
        name=data.name.strip(), low_mhz=data.low_mhz, high_mhz=data.high_mhz,
        limit_dbm=data.limit_dbm, polarizations=list(data.polarizations),
        status=status)
    s.add(row)
    s.flush()
    # 旧版本的 current_version 指针前移；旧行内容原样保留（不可重写历史）
    for old in s.scalars(select(ProtectedReceiverRow).where(
            ProtectedReceiverRow.id == rid,
            ProtectedReceiverRow.version != version)):
        old.current_version = version
    s.flush()
    return row


def create_receiver(s: Session, data: ReceiverIn) -> ProtectedReceiverRow:
    return _append_version(s, None, data, status="active")


def revise_receiver(s: Session, rid: int, data: ReceiverIn) -> ProtectedReceiverRow:
    cur = get_version(s, rid, None, allow_deleted=True)
    if cur.status == "deleted":
        raise ReceiverError(f"受保护接收机规则 {rid!r} 已删除，不能在其上修订；请新建规则")
    return _append_version(s, rid, data, status="active")


def delete_receiver(s: Session, rid: int) -> ProtectedReceiverRow:
    """删除 = 追加一个 deleted 墓碑新版本；历史版本与报告绑定永不丢失。"""
    cur = get_version(s, rid, None, allow_deleted=True)
    if cur.status == "deleted":
        raise ReceiverError(f"受保护接收机规则 {rid!r} 已处于删除状态")
    data = ReceiverIn(name=cur.name, low_mhz=cur.low_mhz, high_mhz=cur.high_mhz,
                      limit_dbm=cur.limit_dbm, polarizations=list(cur.polarizations))
    return _append_version(s, rid, data, status="deleted")


def staleness(s: Session, bindings: list[dict]) -> tuple[bool, str]:
    """根据报告绑定的 (id, version) 与规则库当前版本比对，得出过期状态。"""
    stale_parts: list[str] = []
    for b in bindings:
        rid, ver = b["id"], b["version"]
        cur_no = _current_version_number(s, rid)
        if cur_no is None:
            stale_parts.append(f"规则 {b.get('name', rid)} 已从库中移除")
            continue
        if cur_no != ver:
            cur_row = get_version(s, rid, cur_no, allow_deleted=True)
            what = "已删除" if cur_row.status == "deleted" else f"已修订到 v{cur_no}"
            stale_parts.append(f"规则 {b.get('name', rid)}（报告绑定 v{ver}）{what}")
    if stale_parts:
        return True, "；".join(stale_parts) + "——报告结果仅代表绑定版本，需重新评估"
    return False, ""
