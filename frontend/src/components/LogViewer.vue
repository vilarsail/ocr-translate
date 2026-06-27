<template>
  <div class="log-viewer">
    <div class="log-toolbar">
      <el-select v-model="stageFilter" size="small" style="width: 160px" placeholder="按阶段过滤">
        <el-option label="全部" value="" />
        <el-option v-for="s in stages" :key="s" :label="s" :value="s" />
      </el-select>
      <el-button size="small" @click="autoScroll = !autoScroll">
        {{ autoScroll ? '暂停滚动' : '自动滚动' }}
      </el-button>
      <el-button size="small" @click="clearView">清空显示</el-button>
      <span class="log-count">共 {{ filtered.length }} 条</span>
    </div>
    <div class="log-body" ref="body">
      <div
        v-for="(line, i) in filtered"
        :key="i"
        :class="['log-line', `lvl-${line.level || 'info'}`]"
      >
        <span class="ts">{{ formatTs(line.ts) }}</span>
        <span class="stage">[{{ line.stage }}]</span>
        <span class="msg">{{ line.message }}</span>
      </div>
      <div v-if="!filtered.length" class="empty">暂无日志</div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { api, wsUrl } from '../api'

const props = defineProps({
  projectId: { type: String, required: true },
  stages: { type: Array, default: () => [] },
})

const stageFilter = ref('')
const autoScroll = ref(true)
const body = ref(null)
const lines = ref([])
const maxSeq = ref(0)
let ws = null
let reconnectTimer = null

const filtered = computed(() =>
  lines.value.filter(l => !stageFilter.value || l.stage === stageFilter.value)
)

function formatTs(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function clearView() {
  lines.value = []
  maxSeq.value = 0
}

function scrollBottom() {
  if (!autoScroll.value) return
  nextTick(() => {
    if (body.value) body.value.scrollTop = body.value.scrollHeight
  })
}

function appendEntry(entry) {
  const seq = entry.seq
  const hasSeq = typeof seq === 'number' && seq > 0
  if (hasSeq) {
    if (seq <= maxSeq.value) return
    maxSeq.value = seq
  }
  lines.value.push(entry)
  if (lines.value.length > 5000) lines.value.splice(0, lines.value.length - 5000)
  scrollBottom()
}

async function loadHistory() {
  try {
    const data = await api.getLogs(props.projectId, null, 5000)
    lines.value = []
    maxSeq.value = 0
    for (const e of data) appendEntry(e)
    scrollBottom()
  } catch (e) {
    /* ignore */
  }
}

function connect() {
  if (ws) { try { ws.close() } catch (e) {} }
  ws = new WebSocket(wsUrl(props.projectId))
  ws.onopen = () => {
    try { ws.send(String(maxSeq.value || 0)) } catch (e) {}
  }
  ws.onmessage = (ev) => {
    try {
      const entry = JSON.parse(ev.data)
      appendEntry(entry)
    } catch (e) {
      /* ignore */
    }
  }
  ws.onclose = () => {
    if (reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 2000)
  }
  ws.onerror = () => { try { ws.close() } catch (e) {} }
}

watch(() => props.projectId, async (id) => {
  if (!id) return
  lines.value = []
  maxSeq.value = 0
  await loadHistory()
  connect()
}, { immediate: true })

watch(filtered, scrollBottom)
</script>

<style scoped>
.log-viewer { display: flex; flex-direction: column; height: 360px; border: 1px solid #e4e7ed; border-radius: 4px; background: #fafafa; }
.log-toolbar { padding: 6px 8px; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid #e4e7ed; background: #f5f7fa; }
.log-count { margin-left: auto; color: #909399; font-size: 12px; }
.log-body { flex: 1; overflow-y: auto; padding: 6px 8px; font-family: 'Menlo', 'Consolas', monospace; font-size: 12px; line-height: 1.5; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.ts { color: #909399; margin-right: 6px; }
.stage { color: #409eff; margin-right: 6px; }
.msg { color: #303133; }
.lvl-warn .msg { color: #e6a23c; }
.lvl-error .msg { color: #f56c6c; }
.empty { color: #c0c4cc; text-align: center; padding: 20px; }
</style>
