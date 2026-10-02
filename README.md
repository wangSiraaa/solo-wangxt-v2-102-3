# 频谱工作台（通信工程教学 · 离线简化模型）

用于比较一组载波频率配置的教学工具：录入少量载波与保护间隔后，绘制频段占用与发射谱，
检查 **频带重叠、保护带不足、掩模尾部越界**，并把各载波向固定保护频率窗的方向性泄漏
在**线性域 (W)** 汇总后评估总量；极化复用由输入规则决定（禁止 / 允许 / 隔离度未知待评估）；
并可用 **OR-Tools CP-SAT** 为载波寻找一组满足载波间隔与受保护接收窗约束的频率位置。

> ⚠️ 纯离线简化模型：**不连接无线电设备，不生成任何发射指令**。掩模与发射谱均为教学
> 用平顶 + 折线衰减的简化数值，不对应任何标准或真实设备指标。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18 + Vite + Plotly.js |
| 后端 | FastAPI + Pydantic + SQLAlchemy 2 |
| 科学计算 | SciPy（掩模分段线性插值、PSD 积分）、NumPy |
| 约束求解 | OR-Tools CP-SAT（1 kHz 频率网格） |
| 数据库 | PostgreSQL（中心频率、带宽、功率、示例掩模、场景、受保护接收机版本、历史分析/规划） |

## 目录

```
backend/   FastAPI 应用
  app/services/units.py     dBm<->W、线性域功率汇总
  app/services/masks.py     掩模折线、发射谱曲线（SciPy interp1d）
  app/services/analysis.py  三类冲突检查（定位到载波对/方向）
  app/services/planner.py   OR-Tools CP-SAT 频率规划
  app/db.py / seed.py       SQLAlchemy 模型与教学演示场景
frontend/  React + Plotly.js
scripts/   start-dev.sh     一键启动（本地 PostgreSQL + 后端 + 前端）
```

## 快速开始

需要本机 PostgreSQL（或修改 `DATABASE_URL`）。提供一个 conda 方式安装的便捷脚本：

```bash
# 1) Python 依赖
pip install -r backend/requirements.txt

# 2) 前端依赖
cd frontend && npm install && cd ..

# 3) 初始化数据库并写入示例掩模 + 教学演示场景
python -m app.seed          # 工作目录为 backend/，或设置 PYTHONPATH

# 4) 启动
( cd backend && uvicorn app.main:app --reload --port 8000 )
( cd frontend && npm run dev )        # http://localhost:5173 ，/api 代理到 8000
```

环境变量：`DATABASE_URL`（默认 `postgresql+psycopg2://postgres@127.0.0.1:5432/spectrum`）、
`CORS_ORIGINS`。

## 计算模型要点

### 1. 冲突检查（`POST /api/analyze`，结果含 bands / spectrum / findings / power_summary）

- **频带重叠 overlap**：两频带边缘净距 `gap = max(low) − min(high) ≤ 0`，报重叠量。
- **保护带不足 guard_shortfall**：`0 < gap < guard_required_mhz`，报净距与缺口。
- **掩模尾部越界 mask_tail（有方向）**：发射机掩模谱落入受害载波频带的功率
  `P = ∫_victim 10^(PSD_dbm/10)·1mW df`（PSD 在线性 W/Hz 域积分），超过限值即报；
  因功率/掩模不同，A→B 与 B→A 分别计算、分别定位。
- **极化复用规则**（针对异极化对）：
  - `forbidden` 禁止：重叠即冲突；
  - `allowed` 允许（已知隔离度足够）：可同频，不报几何/泄漏冲突；
  - `unknown` 待评估：重叠只提示“隔离度未知，复用待评估”，不下违规定性；
  - 同极化对无极化隔离，始终按禁止处理。

### 2. 受保护接收机与版本

受保护接收机规则保存：稳定身份 `receiver_key`、不可变版本号、频率窗、窗内允许总功率阈值、
适用极化、说明。场景只绑定规则身份；`POST /api/analyze` 与 `/api/plan` 解析当前版本，并把
完整规则快照写入 `rule_binding.protected_receivers`。

分析时逐载波计算落入窗内的方向性泄漏（SciPy 在线性 W/Hz 域积分），输出
`protected_receiver_checks[].contributions`，再把所有适用极化载波的 W 相加：

- 单载波贡献越限：`protected_receiver_contribution` / `error`，列出载波与泄漏值；
- 总量越限：`protected_receiver_total` / `error`，列出每个载波贡献；
- 异极化隔离规则为 `unknown` 且存在非零异极化贡献：`pending`，即使 worst-case 线性和越限
  也不误判为实际违规，因为未知隔离不能当作 0 dB，也不能被误报为安全；
- 无待评估因素且总量不越限：`ok`。

规则修订通过新增版本实现，不更新旧行；删除是软删除。历史分析/规划整份 JSON 持久化，继续
绑定旧版本；再次读取时 `rule_freshness` 明确标记 `superseded` 或 `deleted`。重新运行分析/
规划才会绑定当前版本。

### 3. 功率汇总

dB 是对数尺度，**不能直接相加**。先换算
`P_W = Σ 1mW · 10^(p_i/10)`，再 `P_dBm = 10 log10(P_W/1mW)`。
响应里同时给出错误的“dBm 直接求和”数值供课堂对比（例如多个载波 275 dBm vs 正确 36.14 dBm）。

### 4. OR-Tools 规划（`POST /api/plan`）

CP-SAT 在 1 kHz 网格上为每个载波选中心频率，约束：

- 落在给定可用频段内；
- 需要隔离的载波对（同极化 / 禁止 / 待评估）通过布尔析取排序，
  满足 `中心距 ≥ 半宽_i + 半宽_j + 要求边缘净距`；
- 允许复用的极化对不加约束（可同址）；
- `guard_only`：统一用保护间隔；`mask_aware`：对每对载波按**双向**掩模泄漏都不越限
  反算所需净距（含 0.5 dB 规划裕量，保证返回方案在分析口径下必然达标）；
- 对每个受保护接收窗，相关载波共享线性功率预算（阈值除以载波数）；若适用极化之间存在未知
  隔离，规划器保守要求这些载波位于掩模跨度之外（对窗零泄漏），使规划后复核不留下待评估；
- 目标：最小化相对录入位置的总偏移。无解时返回 `INFEASIBLE` 与放宽建议。
  返回中带 `post_check`（对方案重新跑一遍完整分析）。

## 教学演示场景（种子数据，12 个载波，带宽均为 4 MHz、功率不同）

| 载波对 | 现象 |
|---|---|
| C1(30 dBm,loose) / C2(20 dBm,loose)，净距 0.5 MHz | 保护带不足 + 双向尾部越界，泄漏 15.4 vs 5.3 dBm（功率不同导致方向不对称） |
| C4(30,strict) / C5(25,loose)，净距 0.5 MHz | 保护带不足；仅弱载波 loose 尾部越界（强→弱 −8.6 dBm 达标边界，弱→强 10.3 dBm 越界） |
| C7(30,loose) / C8(20,strict)，净距 2 MHz | 保护带虽够，强载波拖尾仍越界 2.0 dBm；反向 −47.5 dBm 达标 |
| C9 / C10，重叠 0.5 MHz | 同极化频带重叠 |
| C1(H) / C6(V) 同频 | 规则 unknown → “复用待评估” |
| C11(RHCP) / C12(V) 同频 | 规则 allowed → 无冲突 |

## 测试

```bash
# 纯函数 + 规划 + API（API 测试用内存 SQLite，无需外部服务）
PYTHONPATH=backend pytest backend/tests -q
```

测试覆盖：dBm/W 换算、三类冲突对定位、泄漏方向性、三种极化规则、受保护窗单载波贡献/
线性总量/未知隔离待评估、规则修订后的历史复现与过期标记、两种规划模式的可行性、
保护窗 post-check 与窄资源不可行、场景 CRUD 等。
