"""频带冲突分析（纯函数、离线简化模型）。

检查三类载波间冲突，每条结果都能定位到具体载波（或载波对）：

1. overlap        频带重叠：相邻边缘间隙 <= 0
2. guard_shortfall 保护带不足：0 < 边缘间隙 < 要求保护间隔
3. mask_tail      掩模尾部越界：发射机掩模拖尾落入对方频带内的功率超过限值

此外检查受保护接收机：先逐载波计算落入保护频率窗的方向性泄漏，再把功率
在线性域 (W) 汇总。异极化隔离度未知时，结果只能是“待评估”，不能把
worst-case 总和直接当成安全结论；若不考虑隔离的线性总和本身已越限，则
该总量风险是确定的。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from scipy.integrate import trapezoid as scipy_trapezoid

from .masks import get_mask, spectrum_curve
from .units import dbm_to_watt, total_power_watt, watt_to_dbm

Polarization = Literal["H", "V", "LHCP", "RHCP"]
ReusePolicy = Literal["forbidden", "allowed", "unknown"]

POLARIZATIONS: tuple[str, ...] = ("H", "V", "LHCP", "RHCP")


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


@dataclass(frozen=True)
class ProtectedReceiver:
    receiver_key: str
    version: int
    name: str
    window_low_mhz: float
    window_high_mhz: float
    power_limit_dbm: float
    polarizations: tuple[str, ...]
    description: str = ""

    @property
    def low(self) -> float:
        return self.window_low_mhz

    @property
    def high(self) -> float:
        return self.window_high_mhz


@dataclass
class AnalysisRules:
    # 要求的最小保护间隔 (MHz)：相邻频带边缘之间的净距
    guard_required_mhz: float = 1.0
    # 掩模尾部落入邻载波频带的功率限值 (dBm)
    leakage_limit_dbm: float = -45.0
    # 不同极化对的复用规则，key 形如 "H|V"（极化名排序后拼接）
    # forbidden / allowed / unknown；查不到按 unknown 处理
    reuse_policy: Optional[dict[str, str]] = None
    protected_receivers: Optional[list[ProtectedReceiver]] = None

    def policy_for(self, pol_a: str, pol_b: str) -> ReusePolicy:
        if self.reuse_policy is None:
            return "unknown"
        key = "|".join(sorted((pol_a, pol_b)))
        return self.reuse_policy.get(key, "unknown")  # type: ignore[return-value]


def edge_gap_mhz(a: Carrier, b: Carrier) -> float:
    """两个频带边缘之间的净距 (MHz)；正值=净空，0=相切，负值=重叠。"""
    return max(a.low, b.low) - min(a.high, b.high)


def _integrate_window_psd(tx: Carrier, low_mhz: float, high_mhz: float,
                          grid_step_mhz: float = 0.01) -> float:
    """tx 的 PSD 在线性 W/Hz 域上积分到任意频率窗，返回 W。"""
    mask = get_mask(tx.mask_name)
    f, psd = spectrum_curve(mask, tx.center_mhz, tx.bandwidth_mhz,
                            tx.power_dbm, grid_step_mhz)
    inside = (f >= low_mhz - 1e-12) & (f <= high_mhz + 1e-12)
    if not np.any(inside):
        return 0.0
    psd_w_hz = dbm_to_watt(psd[inside])
    df_hz = grid_step_mhz * 1e6
    # 显式使用 SciPy 积分，避免把 dB 值直接相加。
    return float(scipy_trapezoid(psd_w_hz, dx=df_hz))


def leakage_power_dbm(tx: Carrier, victim: Carrier,
                      grid_step_mhz: float = 0.01) -> float:
    """tx 载波的掩模发射谱落入 victim 频带内的总功率 (dBm)。"""
    p_w = _integrate_window_psd(tx, victim.low, victim.high, grid_step_mhz)
    return watt_to_dbm(p_w) if p_w > 0.0 else float("-inf")


def leakage_to_window_dbm(tx: Carrier, low_mhz: float, high_mhz: float,
                          grid_step_mhz: float = 0.01) -> float:
    """tx 载波的掩模发射谱落入固定频率窗的总功率 (dBm)。"""
    p_w = _integrate_window_psd(tx, low_mhz, high_mhz, grid_step_mhz)
    return watt_to_dbm(p_w) if p_w > 0.0 else float("-inf")


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


def _receiver_ref(rx: ProtectedReceiver) -> dict:
    return {
        "receiver_key": rx.receiver_key,
        "version": rx.version,
        "receiver_name": rx.name,
        "window_low_mhz": rx.window_low_mhz,
        "window_high_mhz": rx.window_high_mhz,
        "power_limit_dbm": rx.power_limit_dbm,
        "polarizations": list(rx.polarizations),
    }


def analyze_protected_receivers(carriers: list[Carrier],
                                rules: AnalysisRules) -> tuple[list[dict], list[dict]]:
    """逐保护窗汇总各载波方向性泄漏，返回 (findings, checks)。"""
    findings: list[dict] = []
    checks: list[dict] = []

    for rx in rules.protected_receivers or []:
        applicable = [c for c in carriers if c.polarization in rx.polarizations]
        contributions = []
        total_w = 0.0
        for tx in applicable:
            leak_dbm = leakage_to_window_dbm(tx, rx.low, rx.high)
            leak_w = dbm_to_watt(leak_dbm)
            total_w += leak_w
            item = {
                "carrier": tx.name,
                "polarization": tx.polarization,
                "mask_name": tx.mask_name,
                "leakage_dbm": None if not np.isfinite(leak_dbm) else round(leak_dbm, 3),
                "leakage_w": leak_w,
                "excess_dbm": (round(leak_dbm - rx.power_limit_dbm, 3)
                               if np.isfinite(leak_dbm) else None),
                "exceeds_limit": bool(np.isfinite(leak_dbm)
                                      and leak_dbm > rx.power_limit_dbm),
            }
            contributions.append(item)

            if item["exceeds_limit"]:
                findings.append({
                    "type": "protected_receiver_contribution",
                    "severity": "error",
                    "carrier_a": tx.name,
                    "carrier_b": rx.name,
                    "message": f"{tx.name} 单独泄漏到受保护接收机 {rx.name} 的频率窗 "
                               f"为 {leak_dbm:.1f} dBm，超过阈值 {rx.power_limit_dbm:.1f} dBm",
                    **_receiver_ref(rx),
                    "leakage_dbm": round(leak_dbm, 3),
                    "excess_dbm": round(leak_dbm - rx.power_limit_dbm, 3),
                })

        active = [x for x in contributions if x["leakage_w"] > 0.0]
        active_pols = {x["polarization"] for x in active}
        unknown_pairs = []
        if len(active_pols) >= 2:
            for x, y in itertools.combinations(active, 2):
                if x["polarization"] == y["polarization"]:
                    continue
                policy = rules.policy_for(x["polarization"], y["polarization"])
                if policy == "unknown":
                    unknown_pairs.append("|".join(sorted((x["carrier"], y["carrier"]))))

        total_dbm = watt_to_dbm(total_w) if total_w > 0.0 else float("-inf")
        total_excess = (total_dbm - rx.power_limit_dbm
                        if np.isfinite(total_dbm) else None)
        total_exceeds = bool(np.isfinite(total_dbm)
                             and total_dbm > rx.power_limit_dbm)
        # 未知隔离无法证明不同极化信号会按同一分量线性相加：即使 worst-case
        # 和值超过阈值，也只能给“待评估”，不能把该上限定性成实际违规；
        # 单载波自身越限已作为独立 error 保留。
        if unknown_pairs:
            severity = "pending"
            status = "polarization_unknown"
            if total_exceeds:
                message = (f"受保护接收机 {rx.name} 的异极化隔离度未知；"
                           f"worst-case 线性总泄漏 {total_dbm:.1f} dBm（高于阈值 "
                           f"{rx.power_limit_dbm:.1f} dBm），实际是否越限需补充隔离度后评估")
            else:
                message = (f"受保护接收机 {rx.name} 的异极化隔离度未知，"
                           f"worst-case 总泄漏 {total_dbm:.1f} dBm，需补充隔离度后评估")
        elif total_exceeds:
            severity = "error"
            status = "total_exceeded"
            message = (f"受保护接收机 {rx.name} 窗内总泄漏 {total_dbm:.1f} dBm，"
                       f"超过阈值 {rx.power_limit_dbm:.1f} dBm（线性域汇总）")
        else:
            severity = "ok"
            status = "ok"
            message = (f"受保护接收机 {rx.name} 窗内总泄漏 {total_dbm:.1f} dBm，"
                       f"低于阈值 {rx.power_limit_dbm:.1f} dBm")

        check = {
            **_receiver_ref(rx),
            "description": rx.description,
            "status": status,
            "worst_case_total_exceeds_limit": total_exceeds,
            "total_power_w": total_w,
            "total_power_dbm": None if not np.isfinite(total_dbm) else round(total_dbm, 3),
            "limit_w": dbm_to_watt(rx.power_limit_dbm),
            "excess_dbm": round(total_excess, 3) if total_excess is not None else None,
            "unknown_polarization_pairs": unknown_pairs,
            "contributions": contributions,
            "message": message,
        }
        checks.append(check)
        if severity in ("error", "pending"):
            findings.append({
                "type": "protected_receiver_total",
                "severity": severity,
                "carrier_a": rx.name,
                "carrier_b": "",
                "message": message,
                **_receiver_ref(rx),
                "total_power_dbm": check["total_power_dbm"],
                "limit_dbm": rx.power_limit_dbm,
                "excess_dbm": check["excess_dbm"],
                "unknown_polarization_pairs": unknown_pairs,
                "contributions": contributions,
            })

    return findings, checks


def analyze(carriers: list[Carrier], rules: AnalysisRules) -> dict:
    """对整组载波做冲突检查、保护窗检查与功率汇总。"""
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

    rx_findings, protected_checks = analyze_protected_receivers(carriers, rules)
    findings.extend(rx_findings)

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
        "protected_receiver_checks": protected_checks,
        "power_summary": summary,
        "counts": counts,
        "status": "conflict" if counts["error"] else ("attention" if (counts["warning"] or counts["pending"]) else "ok"),
    }
