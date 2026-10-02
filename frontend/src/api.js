const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  health: () => request('/health'),
  masks: () => request('/masks'),
  listScenarios: () => request('/scenarios'),
  getScenario: (id) => request(`/scenarios/${id}`),
  listScenarioAnalyses: (id) => request(`/scenarios/${id}/analyses`),
  listScenarioPlans: (id) => request(`/scenarios/${id}/plans`),
  createScenario: (payload) =>
    request('/scenarios', { method: 'POST', body: JSON.stringify(payload) }),
  updateScenario: (id, payload) =>
    request(`/scenarios/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteScenario: (id) =>
    request(`/scenarios/${id}`, { method: 'DELETE' }),
  analyze: (payload) =>
    request('/analyze', { method: 'POST', body: JSON.stringify(payload) }),
  plan: (payload) =>
    request('/plan', { method: 'POST', body: JSON.stringify(payload) }),
  listReceivers: () => request('/protected-receivers'),
  listReceiverVersions: (key) =>
    request(`/protected-receivers/${encodeURIComponent(key)}/versions`),
  createReceiver: (payload) =>
    request('/protected-receivers', { method: 'POST', body: JSON.stringify(payload) }),
  reviseReceiver: (key, payload) =>
    request(`/protected-receivers/${encodeURIComponent(key)}/revisions`, {
      method: 'POST', body: JSON.stringify(payload),
    }),
  deleteReceiver: (key) =>
    request(`/protected-receivers/${encodeURIComponent(key)}`, { method: 'DELETE' }),
  getAnalysis: (id) => request(`/analysis/${id}`),
  getPlan: (id) => request(`/plans/${id}`),
}
