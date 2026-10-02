export default function RuleFreshnessBanner({ freshness }) {
  if (!freshness) return null
  const items = freshness.protected_receivers || []
  const staleItems = items.filter((x) => x.state !== 'current')
  if (!freshness.stale) {
    return (
      <div className="freshness ok">
        ✓ 受保护接收机规则均为当前版本
        {items.map((x) => <span key={x.receiver_key}>{x.receiver_key} v{x.bound_version}</span>)}
      </div>
    )
  }
  return (
    <div className="freshness stale">
      ⚠ {staleItems.some((x) => x.state === 'deleted') ? '规则已删除或' : ''}已修订：
      以下历史结果保留原规则版本以保证可复现；请重新运行分析/规划生成当前版本结果。
      <ul>
        {staleItems.map((x) => (
          <li key={x.receiver_key}>
            <b>{x.receiver_key}</b>：{x.message}
          </li>
        ))}
      </ul>
    </div>
  )
}
