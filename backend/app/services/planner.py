"""OR-Tools 频率位置规划（离线简化模型）。

为给定的一组载波寻找一组满足最小间隔约束的中心频率位置：

- 频率离散到 1 kHz 网格，用 CP-SAT 求解整数模型。
- 同极化、或复用规则为 forbidden/unknown 的极化对：两个频带不得相交，
  且边缘净距不小于要求值；用 AddCircuit 风格的布尔析取实现“i 在 j 左”
  或“j 在 i 左”。
- 复用规则为 allowed（已知隔离度足够）的极化对：允许同频，不加间隔约束。
- guard_only 模式：载波对统一使用规则中的保护间隔。
- mask_aware 模式：每对载波的间隔按双方掩模尾部泄漏都不越限来反算
  （功率/掩模不同 => 两个方向阈值不同）。
- 受保护接收机频率窗：两种模式下都做掩模级避让——单纯保护间隔无法约束
  窗内多条载波拖尾的线性域叠加。每条被计入（同极化/异极化禁止/隔离度未知，
  未知按保守全额处理）的载波都必须整体落在窗左侧或右侧，所需边缘净距按
  “该载波落入窗内的泄漏 ≤ 分摊后单载波预算”反算；预算把窗限值预先扣掉
  0.5 dB 规划裕量与 10·log10(N) 的 N 路均分，保证即使 N 条载波的泄漏同时
  出现，规划后复核的窗内总量仍不越限。

目标：最小化各载波相对其偏好位置（录入中心频率，截断到可用频段内）的偏移量。

只输出频率方案，不连接任何设备，也不产生发射指令。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .analysis import (AnalysisRules, Carrier, ProtectedReceiver, coupling_for)
from .masks import get_mask
from .units import dbm_to_watt, watt_to_dbm

GRID_KHZ = 1  # 频率规划网格：1 kHz
# 泄漏评估网格 (MHz)：必须与分析模块 leakage_power_dbm 的默认网格一致，
# 否则规划求得的“达标净距”在分析口径下可能因数值错位而差之毫厘。
EVAL_GRID_MHZ = 0.01
# 规划裕量 (dB)：泄漏限值在规划口径上收紧 0.5 dB，
# 避免网格边界点的数值取舍让“恰好达标”的方案在分析口径下越限。
PLAN_MARGIN_DB = 0.5


@dataclass
class BandLimits:
    low_mhz: float
    high_mhz: float


def _spectrum_arrays(tx: Carrier, grid_step_mhz: float = EVAL_GRID_MHZ
                     ) -> tuple[np.ndarray, np.ndarray]:
    """tx 的谱线频率与累积功率积分（W），供任意受害窗 O(1) 查窗内泄漏。"""
    from .masks import spectrum_curve
    mask = get_mask(tx.mask_name)
    f, psd = spectrum_curve(mask, tx.center_mhz, tx.bandwidth_mhz,
                            tx.power_dbm, grid_step_mhz)
    psd_w = dbm_to_watt(psd)
    cdf = np.concatenate(([0.0], np.cumsum(psd_w))) * grid_step_mhz * 1e6
    return f, cdf


def _window_leakage_w(f: np.ndarray, cdf: np.ndarray,
                      win_low: float, win_high: float) -> float:
    """由累积积分查任意频率窗内的功率 (W)，边界向保守侧取整网格。"""
    i0 = int(np.searchsorted(f, win_low - 1e-12, side="left"))
    i1 = int(np.searchsorted(f, win_high + 1e-12, side="right"))
    n = len(f)
    if i0 >= n:
        return 0.0
    return float(cdf[min(i1, n)] - cdf[i0])


def _leakage_at_separation(tx: Carrier, victim_bw_mhz: float,
                           separation_mhz: float, grid_step_mhz: float = 0.01) -> float:
    """tx 中心与一个位于其右侧、带宽 victim_bw 的虚拟受害载波中心相距 separation 时，
    落入受害频带的泄漏功率 dBm。"""
    f, cdf = _spectrum_arrays(tx, grid_step_mhz)
    v_low = tx.center_mhz + separation_mhz - victim_bw_mhz / 2.0
    v_high = tx.center_mhz + separation_mhz + victim_bw_mhz / 2.0
    p_w = _window_leakage_w(f, cdf, v_low, v_high)
    return watt_to_dbm(p_w)


def _required_gap_to_window(tx: Carrier, win_bw_mhz: float,
                            limit_dbm: float,
                            grid_step_mhz: float = EVAL_GRID_MHZ) -> float:
    """载波频带在窗左侧（右侧对称）时，使窗内泄漏 <= limit_dbm 的最小边缘净距。

    掩模关于中心对称，左/右两侧所需净距相同。谱线支撑到中心 ±span：
    窗近边达到中心 + span 后泄漏恒为零，扫描必然有解。
    """
    f, cdf = _spectrum_arrays(tx, grid_step_mhz)
    span = get_mask(tx.mask_name).span_mhz
    half = tx.bandwidth_mhz / 2.0
    limit_w = dbm_to_watt(limit_dbm)
    # g 扫描到谱支撑之外一格为止
    max_g = span - half + grid_step_mhz
    gs = np.arange(0.0, max_g + grid_step_mhz / 2.0, grid_step_mhz)
    win_lows = tx.center_mhz + half + gs
    win_highs = win_lows + win_bw_mhz
    i0 = np.searchsorted(f, win_lows - 1e-12, side="left")
    i1 = np.searchsorted(f, win_highs + 1e-12, side="right")
    n = len(f)
    leaks = cdf[np.minimum(i1, n)] - cdf[np.minimum(i0, n)]
    ok = np.where((i0 < n) & (leaks <= limit_w))[0]
    if ok.size == 0:
        return float(np.ceil(max_g / grid_step_mhz + 1e-9) * grid_step_mhz)
    return float(ok[0] * grid_step_mhz)


def _required_gap_one_direction(tx: Carrier, victim: Carrier,
                                rules: AnalysisRules, grid_step_mhz: float = EVAL_GRID_MHZ) -> float:
    """扫描最小边缘净距 g（网格对齐），使 tx 落入 victim 频带的泄漏 <= 限值。

    规划口径将限值收紧 PLAN_MARGIN_DB，保证规划结果在分析口径下留有裕量。
    净距 g 时中心间距 = g + (BW_tx + BW_victim)/2。
    """
    limit = rules.leakage_limit_dbm - PLAN_MARGIN_DB
    max_gap = get_mask(tx.mask_name).span_mhz - victim.bandwidth_mhz / 2.0
    n_steps = int(max_gap / grid_step_mhz) + 1
    for k in range(n_steps + 1):
        g = k * grid_step_mhz
        sep = g + (tx.bandwidth_mhz + victim.bandwidth_mhz) / 2.0
        if _leakage_at_separation(tx, victim.bandwidth_mhz, sep, grid_step_mhz) <= limit:
            return g
    return max_gap


def _safe_edge_gap(a: Carrier, b: Carrier, rules: AnalysisRules) -> float:
    """掩模感知所需的最小边缘净距 (MHz)。

    泄漏与“谁在左”无关：无论排序如何，a->b 与 b->a 两个方向的泄漏都必须达标，
    因此取两个方向所需净距的最大值，同时不小于保护间隔规则。
    结果向上对齐到 10 kHz（EVAL_GRID_MHZ 的整数倍，1 kHz 规划网格可精确实现）。
    """
    g = max(_required_gap_one_direction(a, b, rules),
            _required_gap_one_direction(b, a, rules),
            rules.guard_required_mhz)
    return float(np.ceil(g / EVAL_GRID_MHZ + 1e-9) * EVAL_GRID_MHZ)


def _receiver_budget_limit(rx: ProtectedReceiver, n_counted: int) -> float:
    """N 条被计入载波均分窗限值：扣 0.5 dB 裕量与 10log10(N)，
    使得即使 N 条载波的泄漏同时达到各自预算，窗内总量仍低于限值 0.5 dB。"""
    if n_counted <= 1:
        return rx.limit_dbm - PLAN_MARGIN_DB
    return rx.limit_dbm - PLAN_MARGIN_DB - 10.0 * np.log10(n_counted)


def plan(carriers: list[Carrier], rules: AnalysisRules,
         band: BandLimits, mode: str = "guard_only") -> dict:
    """用 CP-SAT 求一组可行频率位置。"""
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    n = len(carriers)

    # kHz 整数域
    def khz(mhz: float) -> int:
        return int(round(mhz * 1000.0 / GRID_KHZ))

    halfbw = [khz(c.bandwidth_mhz / 2.0) for c in carriers]
    lo = khz(band.low_mhz)
    hi = khz(band.high_mhz)

    x: dict[int, cp_model.IntVar] = {}
    deviation: dict[int, cp_model.IntVar] = {}
    for i, c in enumerate(carriers):
        x[i] = model.new_int_var(lo + halfbw[i], hi - halfbw[i], f"center_{i}")
        pref = min(max(khz(c.center_mhz), lo + halfbw[i]), hi - halfbw[i])
        deviation[i] = model.new_int_var(0, hi - lo, f"dev_{i}")
        model.add_abs_equality(deviation[i], x[i] - pref)

    pair_info: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = carriers[i], carriers[j]
            same_pol = a.polarization == b.polarization
            # 同极化无极化隔离，按禁止同频处理；异极化查输入规则
            policy = "forbidden" if same_pol else rules.policy_for(a.polarization, b.polarization)
            if policy == "allowed":
                pair_info.append({"a": a.name, "b": b.name, "constraint": "co-channel allowed",
                                  "required_edge_mhz": 0.0})
                continue

            if mode == "mask_aware":
                # 双向泄漏都达标所需净距（与排序无关）
                req_edge = khz(_safe_edge_gap(a, b, rules))
                req_i_left = req_j_left = req_edge
            else:
                g = khz(rules.guard_required_mhz)
                req_i_left = req_j_left = g

            # 析取： x_i + half_i + req_i_left + half_j <= x_j
            #   或   x_j + half_j + req_j_left + half_i <= x_i
            lit_ij = model.new_bool_var(f"{i}_left_of_{j}")
            lit_ji = model.new_bool_var(f"{j}_left_of_{i}")
            model.add(x[i] + halfbw[i] + req_i_left + halfbw[j] <= x[j]).only_enforce_if(lit_ij)
            model.add(x[j] + halfbw[j] + req_j_left + halfbw[i] <= x[i]).only_enforce_if(lit_ji)
            # 两个“排在左边”的布尔至少一个为真（二者互斥，覆盖所有排序）
            model.add_bool_or(lit_ij, lit_ji)

            pair_info.append({
                "a": a.name, "b": b.name,
                "constraint": "ordered non-overlap",
                "required_edge_mhz": round(req_i_left * GRID_KHZ / 1000.0, 4),
                "reuse_policy": policy,
            })

    # ---- 受保护接收机频率窗：被计入载波整体避让到窗左侧或右侧 ----
    window_info: list[dict] = []
    for rx in rules.receivers:
        # cross_allowed（已知隔离足够）不约束；其余（含隔离度未知）保守全额计入
        counted_idx = [
            i for i, c in enumerate(carriers)
            if coupling_for(c.polarization, tuple(rx.polarizations), rules) != "cross_allowed"
        ]
        if not counted_idx:
            window_info.append({"receiver": rx.name, "receiver_id": rx.id,
                                "receiver_version": rx.version,
                                "constraint": "no counted carrier",
                                "counted_carriers": [], "required_edge_mhz": 0.0})
            continue
        budget = _receiver_budget_limit(rx, len(counted_idx))
        win_lo = khz(rx.low_mhz)
        win_hi = khz(rx.high_mhz)
        win_bw = rx.bandwidth_mhz
        gaps: list[float] = []
        for i in counted_idx:
            c = carriers[i]
            g_mhz = max(_required_gap_to_window(c, win_bw, budget),
                        rules.guard_required_mhz)
            g_mhz = float(np.ceil(g_mhz / EVAL_GRID_MHZ + 1e-9) * EVAL_GRID_MHZ)
            g = khz(g_mhz)
            gaps.append(g_mhz)
            lit_l = model.new_bool_var(f"{i}_left_of_rx{rx.id}")
            lit_r = model.new_bool_var(f"{i}_right_of_rx{rx.id}")
            # 载波在窗左：x_i + half_i + g <= win_lo
            model.add(x[i] + halfbw[i] + g <= win_lo).only_enforce_if(lit_l)
            # 载波在窗右：win_hi + g + half_i <= x_i
            model.add(win_hi + g + halfbw[i] <= x[i]).only_enforce_if(lit_r)
            model.add_bool_or(lit_l, lit_r)
        window_info.append({
            "receiver": rx.name, "receiver_id": rx.id, "receiver_version": rx.version,
            "window_mhz": [rx.low_mhz, rx.high_mhz],
            "constraint": "ordered avoid window",
            "counted_carriers": [carriers[i].name for i in counted_idx],
            "per_carrier_budget_dbm": round(budget, 2),
            "required_edge_mhz": round(max(gaps), 4),
        })

    model.minimize(sum(deviation.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_search_workers = 4
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "feasible": False,
            "status": solver.status_name(status),
            "message": "在给定频段、间隔与受保护接收机窗约束下找不到可行方案，"
                       "可放宽保护间隔/窗限值、扩大频段或允许极化复用。",
            "assignments": [],
            "pair_constraints": pair_info,
            "window_constraints": window_info,
            "mode": mode,
        }

    assignments = []
    for i, c in enumerate(carriers):
        new_center = solver.value(x[i]) * GRID_KHZ / 1000.0
        assignments.append({
            "name": c.name,
            "original_center_mhz": c.center_mhz,
            "center_mhz": round(new_center, 4),
            "low_mhz": round(new_center - c.bandwidth_mhz / 2.0, 4),
            "high_mhz": round(new_center + c.bandwidth_mhz / 2.0, 4),
            "bandwidth_mhz": c.bandwidth_mhz,
            "power_dbm": c.power_dbm,
            "polarization": c.polarization,
            "mask_name": c.mask_name,
            "shift_mhz": round(new_center - c.center_mhz, 4),
        })

    used = [(p["low_mhz"], p["high_mhz"]) for p in assignments]
    return {
        "feasible": True,
        "status": solver.status_name(status),
        "mode": mode,
        "objective_khz": solver.objective_value * GRID_KHZ,
        "assignments": assignments,
        "pair_constraints": pair_info,
        "window_constraints": window_info,
        "occupied_span_mhz": round(max(h for _, h in used) - min(l for l, _ in used), 4),
        "band_limits_mhz": [band.low_mhz, band.high_mhz],
        "message": f"已找到可行频率位置（共 {n} 个载波，目标偏移 "
                   f"{solver.objective_value * GRID_KHZ:.0f} kHz）。",
    }
