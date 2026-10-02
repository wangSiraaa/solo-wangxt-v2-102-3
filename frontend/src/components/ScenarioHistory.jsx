export default function ScenarioHistory({ analyses = [], plans = [], onOpenAnalysis, onOpenPlan }) {
  const items = [
    ...analyses.map((x) => ({ ...x, label: `分析 #${x.id} · ${x.status}` })),
    ...plans.map((x) => ({
      ...x,
      label: `规划 #${x.id} · ${x.feasible ? '可行' : '不可行'}` +
        (x.status ? ` / ${x.status}` : ''),
    })),
  ].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))

  if (!items.length) {
    return <div className="muted">保存或加载场景后，运行分析/规划会在此形成可复现历史。</div>
  }
  return (
    <div className="history-list">
      {items.map((x) => (
        <div key={`${x.kind}-${x.id}`} className={`history-item ${x.stale ? 'stale' : ''}`}>
          <button type="button" onClick={() =>
            x.kind === 'analysis' ? onOpenAnalysis(x.id) : onOpenPlan(x.id)}>
            {x.label}
          </button>
          <span className="muted">{new Date(x.created_at).toLocaleString()}</span>
          {x.stale && <span className="history-stale">规则过期 · 重新评估</span>}
        </div>
      ))}
    </div>
  )
}
