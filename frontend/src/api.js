import axios from 'axios'

// 后端 API 长前缀（与 backend/app/config.py 的 API_PREFIX 必须完全一致）。
const API_PREFIX = '/ocrsys-7e2f9a4d1c8b3e6f5a0d2b9c7e4f1a8d3b6e9c0d5a2f7b8e1c4d6a9f3b5e0c2d'

// 本地调试：用相对前缀，请求由 vite dev server 代理到 127.0.0.1:7013。
// 线上：把下面一行注释掉，启用再下面那行绝对 URL 直连后端。
const API_BASE = API_PREFIX
// const API_BASE = 'http://YOUR_HOST:7013' + API_PREFIX

const http = axios.create({
  baseURL: API_BASE,
  timeout: 60000,
})

export const api = {
  health: () => http.get('/health').then(r => r.data),
  apiKeys: () => http.get('/apikeys').then(r => r.data),
  reloadApiKeys: () => http.post('/apikeys/reload').then(r => r.data),
  listProjects: () => http.get('/projects').then(r => r.data),
  createProject: (form) => http.post('/projects', form).then(r => r.data),
  getProject: (id) => http.get(`/projects/${id}`).then(r => r.data),
  deleteProject: (id) => http.delete(`/projects/${id}`).then(r => r.data),
  getFile: (id, path) => http.get(`/projects/${id}/files`, { params: { path }, responseType: 'text' }).then(r => r.data),
  getLogs: (id, stage, tail = 200) => http.get(`/projects/${id}/logs`, { params: { stage, tail } }).then(r => r.data),
  getStatus: (id) => http.get(`/projects/${id}/status`).then(r => r.data),
  runStage: (id, stage, body = {}) => http.post(`/projects/${id}/stage/${stage}`, body).then(r => r.data),
  stopStage: (id, stage) => http.post(`/projects/${id}/stage/${stage}/stop`).then(r => r.data),
  downloadProject: (id) => http.get(`/projects/${id}/download`, { responseType: 'blob' }).then(r => r.data),
}

export function wsUrl(projectId) {
  if (API_BASE.startsWith('http://') || API_BASE.startsWith('https://')) {
    const u = new URL(API_BASE)
    const proto = u.protocol === 'https:' ? 'wss' : 'ws'
    const base = u.pathname.replace(/\/$/, '')
    return `${proto}://${u.host}${base}/projects/${projectId}/logs`
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${API_BASE}/projects/${projectId}/logs`
}
