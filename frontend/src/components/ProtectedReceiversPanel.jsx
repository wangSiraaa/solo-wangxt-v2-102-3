import { useState } from 'react'
import { api } from '../api.js'

const POLS = ['H', 'V', 'LHCP', 'RHCP']

const blank = {
  receiver_key: '', name: '', window_low_mhz: 185, window_high_mhz: 190,
  power_limit_dbm: -55, polarizations: ['H'], description: '',
}

export default function ProtectedReceiversPanel({ receivers, selectedKeys, onToggle,
                                                    onRefresh, disabled }) {
  const [form, setForm] = useState(blank)
  const [revisionKey, setRevisionKey] = useState('')
  const [revision, setRevision] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const set = (patch) => setForm((f) => ({ ...f, ...patch }))
  const setRev = (patch) => setRevision((f) => ({ ...f, ...patch }))
  const togglePolIn = (target, setter) => setter((f) => ({
    ...f,
    polarizations: f.polarizations.includes(target)
      ? f.polarizations.filter((x) => x !== target)
      : [...f.polarizations, target],
  }))

  const call = async (kind, fn) => {
    setBusy(kind); setError(''); setMessage('')
    try {
      await fn()
      await onRefresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const create = () => call('create', async () => {
    if (!form.receiver_key.trim()) throw new Error('请填写规则 key')
    if (!form.name.trim()) throw new Error('请填写规则名称')
    if (form.polarizations.length === 0) throw new Error('至少选择一个适用极化')
    if (form.window_high_mhz <= form.window_low_mhz) throw new Error('频率窗上界必须大于下界')
    await api.createReceiver(form)
    setForm(blank)
    setMessage('已创建 v1，历史结果不会被改写。')
  })

  const beginRevision = (rx) => {
    setRevisionKey(rx.receiver_key)
    setRevision({
      name: rx.name, window_low_mhz: rx.window_low_mhz,
      window_high_mhz: rx.window_high_mhz, power_limit_dbm: rx.power_limit_dbm,
      polarizations: [...rx.polarizations], description: rx.description || '',
    })
    setError(''); setMessage('')
  }

  const submitRevision = () => call(`revise-${revisionKey}`, async () => {
    if (!revision?.name.trim()) throw new Error('请填写规则名称')
    if (revision.polarizations.length === 0) throw new Error('至少选择一个适用极化')
    if (revision.window_high_mhz <= revision.window_low_mhz) throw new Error('频率窗上界必须大于下界')
    await api.reviseReceiver(revisionKey, revision)
    setRevisionKey(''); setRevision(null)
    setMessage('规则已修订；旧报告仍可复现，并明确显示需要重新评估。')
  })

  const remove = async (rx) => {
    if (!window.confirm(`软删除 ${rx.receiver_key}？历史分析/计划会保留并标记规则已删除。`)) return
    await call(`delete-${rx.receiver_key}`, async () => {
      await api.deleteReceiver(rx.receiver_key)
      setMessage('已软删除；历史结果仍可复现。')
    })
  }

  return (
    <div>
      <div className="rx-list">
        {receivers.length === 0 && <div className="muted">暂无受保护接收机规则。</div>}
        {receivers.map((rx) => (
          <div key={rx.receiver_key}>
            <label className={`rx-card ${selectedKeys.includes(rx.receiver_key) ? 'selected' : ''}`}>
              <input type="checkbox" checked={selectedKeys.includes(rx.receiver_key)}
                     disabled={disabled} onChange={() => onToggle(rx.receiver_key)} />
              <span className="rx-main">
                <b>{rx.name}</b>
                <span className="muted">{rx.receiver_key} · v{rx.version}</span>
                <span>{rx.window_low_mhz}–{rx.window_high_mhz} MHz · ≤ {rx.power_limit_dbm} dBm · {rx.polarizations.join('/')}</span>
              </span>
              <button type="button" disabled={!!busy} onClick={() => beginRevision(rx)}>修订</button>
              <button type="button" className="danger" disabled={!!busy} onClick={() => remove(rx)}>删除</button>
            </label>
            {revisionKey === rx.receiver_key && revision && (
              <div className="rx-revision">
                <div className="muted">创建新版本 v{rx.version + 1}（v{rx.version} 保留不变）</div>
                <input className="field" value={revision.name} disabled={!!busy}
                       onChange={(e) => setRev({ name: e.target.value })} />
                <div className="row">
                  <input className="field" type="number" value={revision.window_low_mhz}
                         disabled={!!busy} onChange={(e) => setRev({ window_low_mhz: Number(e.target.value) })} />
                  <span className="muted">–</span>
                  <input className="field" type="number" value={revision.window_high_mhz}
                         disabled={!!busy} onChange={(e) => setRev({ window_high_mhz: Number(e.target.value) })} />
                  <span className="muted">MHz</span>
                </div>
                <div className="row">
                  <label className="field-label">总量阈值</label>
                  <input className="field" type="number" value={revision.power_limit_dbm}
                         disabled={!!busy} onChange={(e) => setRev({ power_limit_dbm: Number(e.target.value) })} />
                  <span className="muted">dBm</span>
                </div>
                <div className="row">
                  {POLS.map((p) => (
                    <label key={p} className="check-pill">
                      <input type="checkbox" checked={revision.polarizations.includes(p)}
                             disabled={!!busy} onChange={() => togglePolIn(p, setRevision)} />{p}
                    </label>
                  ))}
                </div>
                <input className="field" value={revision.description} disabled={!!busy}
                       onChange={(e) => setRev({ description: e.target.value })} />
                <div className="row">
                  <button className="primary" disabled={!!busy} onClick={submitRevision}>保存新版本</button>
                  <button disabled={!!busy} onClick={() => { setRevisionKey(''); setRevision(null) }}>取消</button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="rx-form">
        <div className="muted" style={{ marginBottom: 6 }}>新建受保护接收机（首版本不可变）</div>
        <input className="field" placeholder="key，如 rx-telemetry" value={form.receiver_key}
               disabled={disabled || !!busy} onChange={(e) => set({ receiver_key: e.target.value })} />
        <input className="field" placeholder="名称" value={form.name}
               disabled={disabled || !!busy} onChange={(e) => set({ name: e.target.value })} />
        <div className="row">
          <input className="field" type="number" value={form.window_low_mhz}
                 disabled={disabled || !!busy} onChange={(e) => set({ window_low_mhz: Number(e.target.value) })} />
          <span className="muted">–</span>
          <input className="field" type="number" value={form.window_high_mhz}
                 disabled={disabled || !!busy} onChange={(e) => set({ window_high_mhz: Number(e.target.value) })} />
          <span className="muted">MHz</span>
        </div>
        <div className="row">
          <label className="field-label">总量阈值</label>
          <input className="field" type="number" value={form.power_limit_dbm}
                 disabled={disabled || !!busy} onChange={(e) => set({ power_limit_dbm: Number(e.target.value) })} />
          <span className="muted">dBm</span>
        </div>
        <div className="row">
          {POLS.map((p) => (
            <label key={p} className="check-pill">
              <input type="checkbox" checked={form.polarizations.includes(p)}
                     disabled={disabled || !!busy} onChange={() => togglePolIn(p, setForm)} />{p}
            </label>
          ))}
        </div>
        <input className="field" placeholder="说明（可选）" value={form.description}
               disabled={disabled || !!busy} onChange={(e) => set({ description: e.target.value })} />
        <button className="primary" disabled={disabled || !!busy} onClick={create}>
          {busy === 'create' ? '创建中…' : '创建规则'}
        </button>
      </div>
      {error && <div className="err-msg">{error}</div>}
      {message && <div className="ok-msg">{message}</div>}
      <div className="hint">修订会生成新版本；旧分析/计划继续绑定旧版本并显示“需要重新评估”。</div>
    </div>
  )
}
