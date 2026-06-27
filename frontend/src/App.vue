<template>
  <el-container class="app">
    <el-header class="header">
      <div class="brand">PDF 转码系统</div>
      <div class="header-right">
        <el-tag :type="apiKeys.loaded ? 'success' : 'danger'" size="small">
          API Key：{{ apiKeys.loaded ? `已加载 ${apiKeys.count} 个` : '未加载' }}
        </el-tag>
        <el-button size="small" @click="reloadKeys">重载 Key</el-button>
      </div>
    </el-header>

    <el-container>
      <el-aside width="280px" class="aside">
        <div class="aside-section">
          <div class="aside-title">上传原始 PDF</div>
          <el-upload
            :auto-upload="true"
            :show-file-list="false"
            :http-request="onUpload"
            accept=".pdf"
          >
            <el-button type="primary" size="small" :loading="uploading">选择 PDF 上传</el-button>
          </el-upload>
        </div>
        <div class="aside-section">
          <div class="aside-title">项目列表</div>
          <el-menu :default-active="currentId" @select="selectProject">
            <el-menu-item v-for="p in projects" :key="p.id" :index="p.id">
              <div class="proj-item">
                <span class="proj-name">{{ p.name }}</span>
                <span class="proj-pages">{{ p.pdf_pages || 0 }} 页</span>
                <el-button
                  link
                  size="small"
                  type="danger"
                  @click.stop="removeProject(p.id)"
                >删除</el-button>
              </div>
            </el-menu-item>
            <el-empty v-if="!projects.length" description="无项目" :image-size="60" />
          </el-menu>
        </div>
      </el-aside>

      <el-main class="main">
        <div v-if="!current" class="empty-main">
          <el-empty description="请上传 PDF 创建项目，或从左侧选择已有项目" />
        </div>
        <template v-else>
          <div class="proj-header">
            <h2>{{ current.name }}</h2>
            <span class="proj-id">ID: {{ current.id }}</span>
            <el-tag size="small">PDF {{ current.pdf_pages || 0 }} 页</el-tag>
            <el-tag size="small">{{ new Date(current.created_at * 1000).toLocaleString() }}</el-tag>
            <el-button size="small" type="success" plain :loading="downloading" @click="onDownload">下载项目</el-button>
          </div>

          <StagePanel
            v-for="step in steps"
            :key="step.key"
            :project-id="current.id"
            :step="step"
            :stage-status="current.stages[step.key]"
            :running="running.has(step.key)"
            :can-run="canRun(step.key)"
            :files="current.files"
            :pdf-pages="current.pdf_pages || 0"
            @run="onRunStage"
            @stop="onStopStage"
            @view-file="onViewFile"
          />

          <div class="log-section">
            <div class="section-title">实时日志</div>
            <LogViewer :project-id="current.id" :stages="steps.map(s => s.key)" />
          </div>
        </template>
      </el-main>
    </el-container>

    <FileViewer v-model="fileViewerVisible" :project-id="current?.id || ''" :path="viewingPath" />
  </el-container>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from './api'
import StagePanel from './components/StagePanel.vue'
import LogViewer from './components/LogViewer.vue'
import FileViewer from './components/FileViewer.vue'

const projects = ref([])
const currentId = ref('')
const current = ref(null)
const apiKeys = reactive({ loaded: false, count: 0 })
const uploading = ref(false)
const running = reactive(new Set())
const fileViewerVisible = ref(false)
const viewingPath = ref('')
const downloading = ref(false)

const steps = [
  {
    key: 'toc-ocr', index: 1, name: '识别目录内容',
    desc: '识别目录页（指定起止页），逐页输出 toc-ocr/N.txt。已存在的页会跳过，支持断点续跑。',
    fields: [
      { key: 'start_page', label: '起始页', type: 'number', default: 1, min: 1 },
      { key: 'end_page', label: '终点页', type: 'number', default: 1, min: 1 },
      { key: 'backend', label: 'OCR 引擎', type: 'select', default: 'paddle',
        options: [
          { value: 'paddle', label: 'PaddleOCR v6（本地 CPU，省内存）' },
          { value: 'glm', label: 'glm-ocr（ollama 远程）' },
        ],
        help: '小内存服务器选 PaddleOCR；本机有 ollama+glm-ocr 可选 glm-ocr' },
    ],
    filePattern: ['toc-ocr/'],
  },
  {
    key: 'toc-structure', index: 2, name: '识别目录结构',
    desc: '调用大模型（think 模式）分析 toc-ocr/*.txt，输出层级化目录树 toc-structure.json，含 level / title / page_hint / match_keywords。',
    fields: [],
    filePattern: ['toc-structure.json'],
  },
  {
    key: 'body-ocr', index: 3, name: '识别正文',
    desc: '识别正文页（指定起止页），逐页输出 body-ocr/N.txt。已存在的页会跳过，支持断点续跑。',
    fields: [
      { key: 'start_page', label: '起始页', type: 'number', default: 1, min: 1 },
      { key: 'end_page', label: '终点页', type: 'number', default: 1, min: 1 },
      { key: 'backend', label: 'OCR 引擎', type: 'select', default: 'paddle',
        options: [
          { value: 'paddle', label: 'PaddleOCR v6（本地 CPU，省内存）' },
          { value: 'glm', label: 'glm-ocr（ollama 远程）' },
        ],
        help: '小内存服务器选 PaddleOCR；本机有 ollama+glm-ocr 可选 glm-ocr' },
    ],
    filePattern: ['body-ocr/'],
  },
  {
    key: 'organize', index: 4, name: '组织正文',
    desc: '填一对锚点（PDF 页 ↔ 印刷页码），按线性偏移把 TOC 印刷页码映射到 PDF 物理页号；5a 用 LLM 在 ±3 页窗口内精确定位每章起始；5b 按叶子节点（最深层标题）逐节送 LLM 整理为 Markdown（#/##/###），输出 chapters/N-chapter.md。超过 8 页的叶子分批处理，支持断点续传。',
    fields: [
      { key: 'pdf_start', label: 'PDF 起始页', type: 'number', default: 1, min: 1, help: '正文第一页对应的 PDF 物理页号（含前言/目录的偏移）' },
      { key: 'printed_start', label: '印刷起始页', type: 'number', default: 1, min: 1, help: '该 PDF 页上印的页码（通常正文从 1 开始）' },
    ],
    filePattern: ['chapters/'],
  },
  {
    key: 'verify', index: 5, name: '校验',
    desc: '逐叶子比对组织后的 Markdown 与原始 OCR：字数比 + 3-gram recall/precision（≥0.85），抓丢内容、重复、幻觉。输出 chapters/verify-report.json。「执行检查」只校验；「修复异常」删除异常 fragment 并用更小批次重做后复检；「循环修复」重复检查-修复直到 0 异常或达到最大轮次。',
    fields: [
      { key: 'ratio_min', label: '字数比下限', type: 'number', default: 0.90, min: 0.5, max: 1.0, step: 0.01, precision: 2, help: '低于此值判为丢内容（默认 0.90；正常章 0.96-1.00，小叶子 OCR 清理可能到 0.84）' },
      { key: 'ratio_max', label: '字数比上限', type: 'number', default: 1.10, min: 1.0, max: 2.0, step: 0.01, precision: 2, help: '高于此值判为重复/幻觉（默认 1.10）' },
    ],
    actions: [
      { key: 'check', label: '执行检查', mode: 'check', primary: true },
      { key: 'fix', label: '修复异常', mode: 'fix' },
      { key: 'loop', label: '循环修复', mode: 'loop' },
    ],
    filePattern: ['verify-report.json'],
  },
  {
    key: 'export-md', index: 6, name: '合并导出',
    desc: '按章节顺序合并 chapters/*-chapter.md 为 merged/book.md，章节间用 --- 分隔。同步瞬时任务。',
    fields: [],
    filePattern: ['merged/'],
  },
]

const prereq = {
  'toc-ocr': [],
  'toc-structure': ['toc-ocr'],
  'body-ocr': [],
  'organize': ['toc-structure', 'body-ocr'],
  'verify': ['organize'],
  'export-md': ['organize'],
}

function canRun(stage) {
  if (running.has(stage)) return false
  const reqs = prereq[stage] || []
  return reqs.every(r => {
    const st = current.value?.stages?.[r]?.status
    return st === 'done'
  })
}

async function loadProjects() {
  projects.value = await api.listProjects()
}

async function loadApiKeys() {
  try {
    const d = await api.apiKeys()
    apiKeys.loaded = d.loaded
    apiKeys.count = d.count
  } catch (e) {
    apiKeys.loaded = false
  }
}

async function reloadKeys() {
  try {
    const d = await api.reloadApiKeys()
    apiKeys.loaded = d.loaded
    apiKeys.count = d.count
    ElMessage.success(`已加载 ${d.count} 个 API Key`)
  } catch (e) {
    ElMessage.error('重载失败：' + (e.response?.data?.detail || e.message))
  }
}

async function selectProject(id) {
  currentId.value = id
  await refreshCurrent()
  await syncRunningFromServer()
  if (running.size > 0) pollStatus()
}

async function refreshCurrent() {
  if (!currentId.value) { current.value = null; return }
  try {
    current.value = await api.getProject(currentId.value)
  } catch (e) {
    current.value = null
  }
}

async function syncRunningFromServer() {
  if (!currentId.value) return
  try {
    const st = await api.getStatus(currentId.value)
    running.clear()
    st.running.forEach(s => running.add(s))
  } catch (e) { /* ignore */ }
}

async function onUpload(req) {
  const form = new FormData()
  form.append('name', req.file.name.replace(/\.pdf$/i, ''))
  form.append('file', req.file)
  uploading.value = true
  try {
    const meta = await api.createProject(form)
    ElMessage.success(`项目已创建，PDF 共 ${meta.pdf_pages} 页`)
    await loadProjects()
    await selectProject(meta.id)
  } catch (e) {
    ElMessage.error('上传失败：' + (e.response?.data?.detail || e.message))
  } finally {
    uploading.value = false
  }
}

async function removeProject(id) {
  try {
    await ElMessageBox.confirm('确认删除该项目及其所有产物？', '确认', { type: 'warning' })
  } catch (e) {
    return
  }
  await api.deleteProject(id)
  if (currentId.value === id) {
    currentId.value = ''
    current.value = null
  }
  await loadProjects()
  ElMessage.success('已删除')
}

async function onRunStage({ stage, body, mode }) {
  running.add(stage)
  try {
    const payload = mode ? { ...body, mode } : { ...body }
    await api.runStage(currentId.value, stage, payload)
    ElMessage.success(`${stage} 已开始执行`)
  } catch (e) {
    ElMessage.error('启动失败：' + (e.response?.data?.detail || e.message))
    running.delete(stage)
    return
  }
  pollStatus()
}

async function onStopStage(stage) {
  if (stage === 'export-md') {
    ElMessage.info('export-md 是瞬时任务，无需停止')
    return
  }
  try {
    await api.stopStage(currentId.value, stage)
    ElMessage.success(`已请求停止 ${stage}，将在当前批次跑完后停止`)
  } catch (e) {
    ElMessage.error('停止失败：' + (e.response?.data?.detail || e.message))
  }
}

let pollTimer = null
function pollStatus() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    await refreshCurrent()
    await syncRunningFromServer()
    if (running.size === 0) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }, 1500)
}

function onViewFile(path) {
  viewingPath.value = path
  fileViewerVisible.value = true
}

async function onDownload() {
  if (!currentId.value) return
  downloading.value = true
  try {
    const blob = await api.downloadProject(currentId.value)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${currentId.value}.zip`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success('已开始下载')
  } catch (e) {
    ElMessage.error('下载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    downloading.value = false
  }
}

onMounted(async () => {
  await loadApiKeys()
  await loadProjects()
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.app { height: 100vh; }
.header { background: #2c3e50; color: #fff; display: flex; align-items: center; justify-content: space-between; }
.brand { font-size: 18px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 10px; }
.aside { background: #f5f7fa; border-right: 1px solid #e4e7ed; padding: 12px; overflow-y: auto; }
.aside-section { margin-bottom: 18px; }
.aside-title { font-size: 13px; color: #606266; margin-bottom: 8px; font-weight: 600; }
.proj-item { display: flex; justify-content: space-between; align-items: center; width: 100%; gap: 6px; }
.proj-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.proj-pages { color: #909399; font-size: 11px; }
.main { padding: 16px; overflow-y: auto; background: #fff; }
.empty-main { display: flex; align-items: center; justify-content: center; height: 100%; }
.proj-header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.proj-header h2 { margin: 0; }
.proj-id { color: #909399; font-size: 12px; }
.log-section { margin-top: 20px; }
.section-title { font-weight: 600; margin-bottom: 8px; color: #303133; }
:deep(.el-menu-item) { height: 40px; line-height: 40px; }
</style>

<style>
html, body, #app { margin: 0; padding: 0; height: 100%; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
</style>
