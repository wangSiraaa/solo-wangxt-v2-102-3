"""频带冲突分析（纯函数、离线简化模型）。

检查三类载波对冲突，每条结果都能定位到具体载波（或载波对）：

1. overlap        频带重叠：相邻边缘间隙 <= 0
2. guard_shortfall 保护带不足：0 < 边缘间隙 < 要求保护间隔
3. mask_tail      掩模尾部越界：发射机掩模拖尾落入对方频带内的功率超过限值

此外对每条“受保护接收机”规则做窗内总量评估：

4. protected_exceed / protected_pending / protected_ok
   场景内每条载波对该频率窗的方向性泄漏逐个计算，在线性域 (W) 汇总后
   再与窗内允许总功率阈值比较；异极化且隔离度未知时只给“待评估”，
   既不能误报为安全，也不能误报为违规。规则以 (id, version) 绑定进结果，
   接收机规则修订/删除后旧结果仍可按原版本复现。

极化复用规则（reuse_policy，针对一对不同极化）：
- "forbidden" 不允许同频复用：重叠时按冲突处理
- "allowed"   已知隔离度足够：重叠允许，不报几何/泄漏冲突
- "unknown"   隔离度未知：几何重叠时只给“待评估”，不下违规定性

功率汇总：线性域 (W) 求和后再换算 dBm 显示，见 units.py。
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

from .masks import get_mask, spectrum_curve
from .units import dbm_to_watt, total_power_watt, watt_to_dbm

Polarization = Literal["H", "V", "LHCP", "RHCP"]
ReusePolicy = Literal["forbidden", "allowed", "unknown"]

POLARIZATIONS: tuple[str, ...] = ("H", "V", "LHCP", "RHCP")

# 载波对保护窗的耦合分类：
# co_pol          载波极化在接收机适用极化内 -> 无隔离，功率全额计入
# cross_forbidden 异极化且规则禁止复用 -> 不给予隔离度抵免，保守全额计入
# cross_unknown   异极化且隔离度未知 -> 不计入总量，但结果降为“待评估”
# cross_allowed   异极化且已知隔离足够 -> 该载波在窗内视为无贡献
Coupling = Literal["co_pol", "cross_forbidden", "cross_unknown", "cross_allowed"]


@dataclass
class Carrier:
    id: Optional[int]
    name: str
    center_mhz: float
    bandwidth_mhz: float
    power_dbm: float
    polarization: str
    mask_name: str

    @property
    def low(self) -> float:
        return self.center_mhz - self.bandwidth_mhz / 2.0

    @property
    def high(self) -> float:
        return self.center_mhz + self.bandwidth_mhz / 2.0


@dataclass
class ProtectedReceiver:
    """受保护接收机规则的一个不可变版本（频率窗 + 允许总功率 + 适用极化）。"""
    id: int
    name: str
    low_mhz: float
    high_mhz: float
    limit_dbm: float
    polarizations: tuple[str, ...]
    version: int = 1
    status: str = "active"  # active / deleted（删除即新版本墓碑，历史版本保留）

    @property
    def bandwidth_mhz(self) -> float:
        return self.high_mhz - self.low_mhz

    def ref(self) -> dict:
        """进入分析结果/报告绑定的版本快照。"""
        return {"id": self.id, "version": self.version, "name": self.name,
                "status": self.status}


@dataclass
class AnalysisRules:
    # 要求的最小保护间隔 (MHz)：相邻频带边缘之间的净距
    guard_required_mhz: float = 1.0
    # 掩模尾部落入邻载波频带的功率限值 (dBm)
    leakage_limit_dbm: float = -45.0
    # 不同极化对的复用规则，key 形如 "H|V"（极化名排序后拼接）
    # forbidden / allowed / unknown；查不到按 unknown 处理
    reuse_policy: Optional[dict[str, str]] = None
    # 本次分析绑定的受保护接收机规则（各自带版本号）
    receivers: list[ProtectedReceiver] = field(default_factory=list)

    def policy_for(self, pol_a: str, pol_b: str) -> ReusePolicy:
        if self.reuse_policy is None:
            return "unknown"
        key = "|".join(sorted((pol_a, pol_b)))
        return self.reuse_policy.get(key, "unknown")  # type: ignore[return-value]


def edge_gap_mhz(a: Carrier, b: Carrier) -> float:
    """两个频带边缘之间的净距 (MHz)；正值=净空，0=相切，负值=重叠。"""
    return max(a.low, b.low) - min(a.high, b.high)


def _integrate_window(tx: Carrier, win_low_mhz: float, win_high_mhz: float,
                      grid_step_mhz: float) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """取 tx 发射谱落在 [win_low, win_high] 内的 (频率网格, PSD W/Hz)；无交集返回 None。"""
    mask = get_mask(tx.mask_name)
    f, psd = spectrum_curve(mask, tx.center_mhz, tx.bandwidth_mhz,
                            tx.power_dbm, grid_step_mhz)
    inside = (f >= win_low_mhz - 1e-12) & (f <= win_high_mhz + 1e-12)
    if not np.any(inside):
        return None
    return f[inside], dbm_to_watt(psd[inside])


def leakage_power_dbm(tx: Carrier, victim: Carrier,
                      grid_step_mhz: float = 0.01) -> float:
    """tx 载波的掩模发射谱落入 victim 频带内的总功率 (dBm)。

    PSD 在线性域 (W/Hz) 上对频率积分：
        P_leak = ∫_victim_band 10**(psd_dbm_hz/10) * 1e-3 df
    df 以 Hz 计。超出掩模跨度的网格点 psd = -inf，贡献为 0。
    """
    part = _integrate_window(tx, victim.low, victim.high, grid_step_mhz)
    if part is None:
        return float("-inf")
    _, psd_w_hz = part
    p_w = float(np.trapezoid(psd_w_hz, dx=grid_step_mhz * 1e6))
    return watt_to_dbm(p_w)


def leakage_into_window_dbm(tx: Carrier, win_low_mhz: float, win_high_mhz: float,
                            grid_step_mhz: float = 0.01) -> float:
    """tx 载波的掩模发射谱落入任意频率窗 [win_low, win_high] 内的总功率 (dBm)。

    与受害载波是否存在无关——受保护接收机只声明频率窗，方向性来自每条
    发射机自身的中心/掩模/功率。无谱线落入窗内时返回 -inf（零贡献）。
    """
    part = _integrate_window(tx, win_low_mhz, win_high_mhz, grid_step_mhz)
    if part is None:
        return float("-inf")
    _, psd_w_hz = part
    p_w = float(np.trapezoid(psd_w_hz, dx=grid_step_mhz * 1e6))
    return watt_to_dbm(p_w)


def coupling_for(tx_pol: str, rx_pols: tuple[str, ...],
                 rules: AnalysisRules) -> Coupling:
    """载波极化对受保护接收机的耦合分类（见模块开头 Coupling 说明）。

    接收机可适用多个极化：载波命中任一适用极化即全额（无隔离）；
    异极化时对每个适用极化查复用规则，取最保守的一档。
    """
    if tx_pol in rx_pols:
        return "co_pol"
    rank = {"allowed": 0, "unknown": 1, "forbidden": 2}
    worst: str = "allowed"
    for rp in rx_pols:
        pol = rules.policy_for(tx_pol, rp)
        if rank[pol] > rank[worst]:
            worst = pol
    if worst == "forbidden":
        return "cross_forbidden"
    if worst == "unknown":
        return "cross_unknown"
    return "cross_allowed"


def _finding(ftype: str, severity: str, a: Carrier, b: Carrier,
             message: str, **metrics) -> dict:
    out = {
        "type": ftype,
        "severity": severity,
        "carrier_a": a.name,
        "carrier_b": b.name,
        "message": message,
    }
    out.update(metrics)
    return out


def _contribution(c: Carrier, rx: ProtectedReceiver, leakage_dbm: float,
                  coupling: Coupling) -> dict:
    return {
        "carrier": c.name,
        "polarization": c.polarization,
        "mask_name": c.mask_name,
        "coupling": coupling,
        "leakage_dbm": round(leakage_dbm, 2),
        "leakage_w": dbm_to_watt(leakage_dbm) if math.isfinite(leakage_dbm) else 0.0,
    }


def evaluate_receiver(carriers: list[Carrier], rx: ProtectedReceiver,
                      rules: AnalysisRules,
                      grid_step_mhz: float = 0.01) -> Optional[dict]:
    """对单条受保护接收机规则评估窗内总功率。

    逐载波计算方向性泄漏（不做 dB 相加），同极化/禁止复用载波的贡献在线性
    域 W 汇总为已知总量；异极化且隔离度未知的载波单列为 unknown 贡献：

    - 已知总量本身已越限        -> error（exceed_kind=single/aggregate），
      未知隔离度再多也不可能翻案；
    - 存在未知隔离度的实质贡献  -> pending（待评估），不报安全也不报违规；
    - 已知总量不越限且无未知项  -> ok。
    没有任何载波谱线落入窗内时返回 None（该窗与本场景无关）。
    """
    known: list[dict] = []
    unknown: list[dict] = []
    total_w = 0.0

    for c in carriers:
        leak = leakage_into_window_dbm(c, rx.low_mhz, rx.high_mhz, grid_step_mhz)
        if not math.isfinite(leak):
            continue  # 掩模谱未触及该窗
        coupling = coupling_for(c.polarization, tuple(rx.polarizations), rules)
        contrib = _contribution(c, rx, leak, coupling)
        if coupling == "cross_allowed":
            continue  # 已知隔离足够：窗内不计该载波
        if coupling == "cross_unknown":
            unknown.append(contrib)
            continue
        known.append(contrib)
        total_w += contrib["leakage_w"]

    if not known and not unknown:
        return None

    known.sort(key=lambda d: d["leakage_w"], reverse=True)
    unknown.sort(key=lambda d: d["leakage_w"], reverse=True)
    total_dbm = watt_to_dbm(total_w) if total_w > 0 else float("-inf")
    max_single_dbm = known[0]["leakage_dbm"] if known else float("-inf")

    base = {
        "receiver_id": rx.id,
        "receiver_version": rx.version,
        "receiver": rx.name,
        "receiver_status": rx.status,
        "window_mhz": [round(rx.low_mhz, 4), round(rx.high_mhz, 4)],
        "polarizations": list(rx.polarizations),
        "limit_dbm": rx.limit_dbm,
        "total_dbm": round(total_dbm, 2) if math.isfinite(total_dbm) else None,
        "total_w": total_w,
        "max_single_dbm": round(max_single_dbm, 2) if math.isfinite(max_single_dbm) else None,
        "contributors": known,
        "unknown_contributors": unknown,
    }

    pols = "/".join(rx.polarizations)
    top = f"，最大单载波 {base['max_single_dbm']} dBm" if known else ""
    if total_w > 0.0 and total_dbm > rx.limit_dbm:
        single = max_single_dbm > rx.limit_dbm
        kind = "single" if single else "aggregate"
        if single:
            msg = (f"受保护接收机 {rx.name}（{pols}，窗 {rx.low_mhz:g}–{rx.high_mhz:g} MHz）"
                   f"被单载波拖尾越限：{known[0]['carrier']} 泄漏 "
                   f"{base['max_single_dbm']} dBm > 限值 {rx.limit_dbm:g} dBm")
        else:
            names = "、".join(d["carrier"] for d in known[:4])
            msg = (f"受保护接收机 {rx.name}（{pols}，窗 {rx.low_mhz:g}–{rx.high_mhz:g} MHz）"
                   f"窗内总量 {base['total_dbm']} dBm > 限值 {rx.limit_dbm:g} dBm："
                   f"各单载波均不越限{top}，但线性域叠加后越限（贡献: {names}）")
        return {**base, "verdict": "exceed", "exceed_kind": kind,
                "severity": "error", "excess_dbm": round(total_dbm - rx.limit_dbm, 2),
                "message": msg}

    if unknown:
        names = "、".join(d["carrier"] for d in unknown[:4])
        known_part = (f"已知极化贡献合计 {base['total_dbm']} dBm 未越限；"
                      if total_w > 0.0 else "")
        msg = (f"受保护接收机 {rx.name}（{pols}，窗 {rx.low_mhz:g}–{rx.high_mhz:g} MHz）"
               f"{known_part}载波 {names} 与接收机极化的隔离度未知，"
               f"窗内总量待评估——不能判定安全，也不能直接判违规")
        return {**base, "verdict": "pending", "exceed_kind": None,
                "severity": "pending", "excess_dbm": None, "message": msg}

    msg = (f"受保护接收机 {rx.name}（{pols}，窗 {rx.low_mhz:g}–{rx.high_mhz:g} MHz）"
           f"窗内总量 {base['total_dbm']} dBm ≤ 限值 {rx.limit_dbm:g} dBm{top}")
    return {**base, "verdict": "ok", "exceed_kind": None,
            "severity": "ok", "excess_dbm": None, "message": msg}


def _receiver_finding(ev: dict) -> dict:
    """把接收机评估结果转成统一 findings 条目（carrier_a 放窗名，carrier_b 放头号贡献）。"""
    top_name = ""
    if ev["contributors"]:
        top_name = ev["contributors"][0]["carrier"]
    elif ev["unknown_contributors"]:
        top_name = ev["unknown_contributors"][0]["carrier"]
    ftype = {"exceed": "protected_exceed",
             "pending": "protected_pending",
             "ok": "protected_ok"}[ev["verdict"]]
    return {
        "type": ftype,
        "severity": ev["severity"],
        "carrier_a": ev["receiver"],
        "carrier_b": top_name,
        "receiver_id": ev["receiver_id"],
        "receiver_version": ev["receiver_version"],
        "message": ev["message"],
        "evaluation": ev,
    }


def analyze(carriers: list[Carrier], rules: AnalysisRules) -> dict:
    """对整组载波做冲突检查、功率汇总与受保护接收机窗内总量评估。"""
    findings: list[dict] = []

    for a, b in itertools.combinations(carriers, 2):
        # 边缘净距：频带不相交时 >0（净空），相切时 0，重叠时 <0
        gap = max(a.low, b.low) - min(a.high, b.high)
        overlap = max(0.0, -gap)
        same_pol = a.polarization == b.polarization
        # 同极化没有极化隔离可言，按禁止同频复用处理；异极化查输入规则
        policy: str = "forbidden" if same_pol else rules.policy_for(a.polarization, b.polarization)
        pol_key = "|".join(sorted((a.polarization, b.polarization)))

        # ---- 几何关系：重叠 / 保护带 ----
        if gap <= 0.0:
            if same_pol:
                findings.append(_finding(
                    "overlap", "error", a, b,
                    f"{a.name} 与 {b.name} 频带重叠 {overlap:.3f} MHz（同极化 {a.polarization}）",
                    gap_mhz=round(gap, 4), overlap_mhz=round(overlap, 4),
                    polarization_pair=pol_key, reuse_policy="forbidden"))
            elif policy == "forbidden":
                findings.append(_finding(
                    "overlap", "error", a, b,
                    f"{a.name} 与 {b.name} 频带重叠 {overlap:.3f} MHz，"
                    f"且极化 {a.polarization}/{b.polarization} 规则禁止同频复用",
                    gap_mhz=round(gap, 4), overlap_mhz=round(overlap, 4),
                    polarization_pair=pol_key, reuse_policy="forbidden"))
            elif policy == "unknown":
                findings.append(_finding(
                    "reuse_unknown", "pending", a, b,
                    f"{a.name} 与 {b.name} 同频段重叠 {overlap:.3f} MHz，"
                    f"极化 {a.polarization}/{b.polarization} 的隔离度未知，复用待评估",
                    gap_mhz=round(gap, 4), overlap_mhz=round(overlap, 4),
                    polarization_pair=pol_key, reuse_policy="unknown"))
            # policy == "allowed"：已知隔离度足够，允许复用，不报冲突
        elif gap < rules.guard_required_mhz:
            if same_pol or policy in ("forbidden", "unknown"):
                findings.append(_finding(
                    "guard_shortfall", "warning", a, b,
                    f"{a.name} 与 {b.name} 边缘净距 {gap:.3f} MHz "
                    f"< 要求保护间隔 {rules.guard_required_mhz:.3f} MHz"
                    + ("" if same_pol else f"（极化 {a.polarization}/{b.polarization}，"
                       f"复用规则: {policy}）"),
                    gap_mhz=round(gap, 4),
                    required_mhz=rules.guard_required_mhz,
                    deficit_mhz=round(rules.guard_required_mhz - gap, 4),
                    polarization_pair=pol_key, reuse_policy=policy))

        # ---- 掩模尾部越界（两个方向都算，功率/掩模不同则结果不对称）----
        # 允许复用（已知隔离足够）或已几何重叠的配对不再重复报尾部泄漏。
        geometrically_overlap = gap <= 0.0
        skip_tail = (policy == "allowed") or geometrically_overlap
        if not skip_tail:
            for tx, victim in ((a, b), (b, a)):
                leak = leakage_power_dbm(tx, victim)
                if leak > rules.leakage_limit_dbm:
                    findings.append(_finding(
                        "mask_tail", "error", tx, victim,
                        f"{tx.name} 的掩模尾部泄漏到 {victim.name} 频带内 "
                        f"{leak:.1f} dBm，超过限值 {rules.leakage_limit_dbm:.1f} dBm",
                        leakage_dbm=round(leak, 2),
                        limit_dbm=rules.leakage_limit_dbm,
                        excess_dbm=round(leak - rules.leakage_limit_dbm, 2),
                        polarization_pair=pol_key, reuse_policy=policy,
                        direction=f"{tx.name}->{victim.name}"))

    # ---- 受保护接收机：逐窗汇总每条载波的方向性泄漏（线性域 W）----
    protected_evaluations: list[dict] = []
    for rx in rules.receivers:
        ev = evaluate_receiver(carriers, rx, rules)
        if ev is None:
            continue
        protected_evaluations.append(ev)
        findings.append(_receiver_finding(ev))

    # ---- 功率汇总：线性域求和 ----
    per_carrier = [
        {"name": c.name, "power_dbm": c.power_dbm, "power_w": dbm_to_watt(c.power_dbm)}
        for c in carriers
    ]
    powers_dbm = [c.power_dbm for c in carriers]
    total_w = total_power_watt(powers_dbm)
    summary = {
        "carrier_count": len(carriers),
        "total_power_w": total_w,
        "total_power_dbm": watt_to_dbm(total_w),
        # 仅供教学对比：直接对 dBm 求和是常见错误做法
        "naive_dbm_sum": round(sum(powers_dbm), 3) if powers_dbm else None,
        "per_carrier": per_carrier,
    }

    counts = {"error": 0, "warning": 0, "pending": 0, "ok": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    return {
        "findings": findings,
        "power_summary": summary,
        "counts": counts,
        "protected_evaluations": protected_evaluations,
        "protected_receivers": [
            {"id": rx.id, "name": rx.name, "version": rx.version, "status": rx.status,
             "low_mhz": rx.low_mhz, "high_mhz": rx.high_mhz,
             "limit_dbm": rx.limit_dbm, "polarizations": list(rx.polarizations)}
            for rx in rules.receivers
        ],
        "status": "conflict" if counts["error"] else ("attention" if (counts["warning"] or counts["pending"]) else "ok"),
    }
