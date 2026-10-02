const STATUS_LABEL = {
  ok: '通过',
  total_exceeded: '总量越限',
  polarization_unknown: '极化隔离未知',
}
const STATUS_CLASS = {
  ok: 'ok',
  total_exceeded: 'error',
  polarization_unknown: 'pending',
}

export default function ProtectedReceiverChecks({ checks = [], windows = [] }) {
  const shown = checks?.length ? checks : []
  if (!shown.length) {
    return <div className="muted">本分析未绑定受保护接收机。</div>
  }
  return (
    <div className="rx-checks">
      {shown.map((rx) => (
        <div key={`${rx.receiver_key}-v${rx.version}`} className={`rx-check ${STATUS_CLASS[rx.status] || ''}`}>
          <div className="row">
            <span className={`tag ${STATUS_CLASS[rx.status] || ''}`}>{STATUS_LABEL[rx.status] || rx.status}</span>
            <b>{rx.receiver_name}</b>
            <span className="muted">{rx.receiver_key} · v{rx.version}</span>
            <span className="spacer" />
            <span className="muted">
              {rx.window_low_mhz}–{rx.window_high_mhz} MHz · 阈值 {rx.power_limit_dbm} dBm
            </span>
          </div>
          <div>{rx.message}</div>
          <div className="muted">
            窗内线性汇总：{rx.total_power_dbm == null ? '无可计入泄漏' : `${rx.total_power_dbm} dBm`}
            {rx.status === 'polarization_unknown' && rx.worst_case_total_exceeds_limit &&
              <b className="error-text">（worst-case 越限，隔离未知不判定实际违规）</b>}
            {rx.excess_dbm != null && ` · 超出 ${rx.excess_dbm} dB`}
            {' '}· 适用极化 {rx.polarizations.join('/')}
          </div>
          {rx.unknown_polarization_pairs?.length > 0 && (
            <div className="meta pending-text">
              待评估载波对：{rx.unknown_polarization_pairs.join('、')}
            </div>
          )}
          <details>
            <summary>单载波方向性泄漏贡献（{rx.contributions.length}）</summary>
            <table className="rx-contrib-table">
              <thead>
                <tr><th>载波</th><th>极化</th><th>泄漏 dBm</th><th>相对阈值</th></tr>
              </thead>
              <tbody>
                {rx.contributions.map((c) => (
                  <tr key={c.carrier} className={c.exceeds_limit ? 'error-text' : ''}>
                    <td>{c.carrier}</td>
                    <td>{c.polarization}</td>
                    <td>{c.leakage_dbm == null ? '无（掩模跨度外）' : c.leakage_dbm}</td>
                    <td>{c.excess_dbm == null ? '—' : `${c.excess_dbm > 0 ? '+' : ''}${c.excess_dbm}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </div>
      ))}
      {windows?.length > 0 && (
        <div className="hint">图中彩色竖纹区域为保护频率窗；红/紫色边框表示越限或待评估。</div>
      )}
    </div>
  )
}
