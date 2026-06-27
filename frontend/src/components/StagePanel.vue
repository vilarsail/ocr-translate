<template>
  <el-card class="stage-panel" shadow="never">
    <template #header>
      <div class="header">
        <div class="title">
          <el-tag :type="statusType" size="small" effect="dark">{{ step.index }}</el-tag>
          <span class="name">{{ step.name }}</span>
          <el-tag v-if="stageStatus" :type="statusType" size="small">{{ statusLabel }}</el-tag>
        </div>
        <div class="stats" v-if="statsText">{{ statsText }}</div>
      </div>
    </template>

    <el-alert v-if="step.desc" :title="step.desc" type="info" :closable="false" class="desc" />

    <el-form v-if="step.fields && step.fields.length" label-width="110px" size="small" class="form">
      <el-form-item v-for="f in step.fields" :key="f.key" :label="f.label">
        <el-input-number
          v-if="f.type === 'number'"
          v-model="form[f.key]"
          :min="f.min ?? 1"
          :max="fieldMax(f)"
          :step="f.step || 1"
          :precision="f.precision"
          style="width: 160px"
        />
        <el-input
          v-else-if="f.type === 'text'"
          v-model="form[f.key]"
          :placeholder="f.placeholder || ''"
          style="width: 320px"
        />
        <el-radio-group v-else-if="f.type === 'radio'" v-model="form[f.key]">
          <el-radio v-for="opt in f.options" :key="opt.value" :value="opt.value">{{ opt.label }}</el-radio>
        </el-radio-group>
        <el-select v-else-if="f.type === 'select'" v-model="form[f.key]" style="width: 280px">
          <el-option v-for="opt in f.options" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>
        <div v-if="f.help" class="help">{{ f.help }}</div>
      </el-form-item>
    </el-form>

    <div class="actions">
      <template v-if="step.actions && step.actions.length">
        <el-button
          v-for="a in step.actions"
          :key="a.key"
          :type="a.primary ? 'primary' : 'default'"
          :disabled="!canRun"
          :loading="running"
          @click="onRun(a.mode)"
        >
          {{ running ? '执行中…' : a.label }}
        </el-button>
      </template>
      <el-button
        v-else
        type="primary"
        :disabled="!canRun"
        :loading="running"
        @click="onRun()"
      >
        {{ running ? '执行中…' : '执行' }}
      </el-button>
      <el-button
        v-if="running"
        type="danger"
        plain
        @click="$emit('stop', step.key)"
      >
        停止
      </el-button>
      <span v-if="!canRun && !running" class="hint">前置阶段未完成</span>
    </div>

    <el-collapse class="results" v-if="resultFiles.length">
      <el-collapse-item title="产物文件（点击查看）" name="files">
        <div class="file-grid">
          <el-button
            v-for="f in resultFiles"
            :key="f.path"
            size="small"
            @click="$emit('viewFile', f.path)"
          >
            {{ f.path }}
            <span class="fsize">({{ humanSize(f.size) }})</span>
          </el-button>
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-card>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  projectId: { type: String, required: true },
  step: { type: Object, required: true },
  stageStatus: { type: Object, default: null },
  running: { type: Boolean, default: false },
  canRun: { type: Boolean, default: true },
  files: { type: Array, default: () => [] },
  pdfPages: { type: Number, default: 0 },
})
const emit = defineEmits(['run', 'viewFile', 'stop'])

const form = ref({})
watch(() => props.step, (s) => {
  const f = {}
  for (const field of (s.fields || [])) {
    f[field.key] = field.default
  }
  form.value = f
}, { immediate: true })

function fieldMax(f) {
  if (typeof f.max === 'number') return f.max
  if (f.key === 'start_page' || f.key === 'end_page') {
    return props.pdfPages > 0 ? props.pdfPages : 9999
  }
  return 9999
}

const statusType = computed(() => {
  const st = props.stageStatus?.status
  if (props.running) return 'warning'
  if (st === 'done') return 'success'
  if (st === 'failed' || st === 'interrupted' || st === 'stopped') return 'danger'
  return 'info'
})
const statusLabel = computed(() => {
  if (props.running) return '执行中'
  const st = props.stageStatus?.status
  return { idle: '未开始', running: '执行中', done: '已完成', failed: '失败', interrupted: '已中断', stopped: '已停止' }[st] || st
})

const statsText = computed(() => {
  const s = props.stageStatus
  if (!s) return ''
  const parts = []
  const isVerify = props.step.key === 'verify'
  if (!isVerify && s.progress) {
    const p = s.progress
    if (p.total !== undefined) parts.push(`${p.done || 0}/${p.total}`)
    if (p.current_page !== undefined) parts.push(`当前第 ${p.current_page} 页`)
    if (p.failed !== undefined) parts.push(`失败 ${p.failed}`)
  }
  if (s.failed_pages !== undefined) parts.push(`失败页 ${JSON.stringify(s.failed_pages)}`)
  if (s.total_entries !== undefined) parts.push(`目录条目 ${s.total_entries}`)
  if (s.level1_count !== undefined) parts.push(`一级标题 ${s.level1_count}`)
  if (s.ok !== undefined && s.bad !== undefined) {
    parts.push(`正常 ${s.ok} · 异常 ${s.bad}`)
    if (s.round !== undefined && s.max_rounds !== undefined) {
      parts.push(`第 ${s.round}/${s.max_rounds} 轮`)
    }
  }
  if (s.bad_chapters !== undefined && s.bad_chapters && s.bad_chapters.length) {
    parts.push(`异常章节 ${s.bad_chapters.join('、')}`)
  }
  if (s.count !== undefined) parts.push(`合并 ${s.count} 文件`)
  if (s.size !== undefined) parts.push(`${s.size} 字符`)
  if (s.error) parts.push(`错误：${s.error}`)
  return parts.join(' · ')
})

const resultFiles = computed(() => {
  if (!props.files || !props.files.length) return []
  return props.files.filter(f => props.step.filePattern?.some(p => f.path.includes(p)))
})

function humanSize(n) {
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`
  return `${(n / 1024 / 1024).toFixed(1)}M`
}

function onRun(mode) {
  emit('run', { stage: props.step.key, body: { ...form.value }, mode })
}
</script>

<style scoped>
.stage-panel { margin-bottom: 16px; }
.header { display: flex; justify-content: space-between; align-items: center; }
.title { display: flex; align-items: center; gap: 8px; }
.name { font-weight: 600; }
.stats { font-size: 12px; color: #606266; }
.desc { margin-bottom: 12px; }
.form { margin: 8px 0; }
.help { font-size: 12px; color: #909399; line-height: 1.4; }
.actions { margin: 8px 0; }
.hint { margin-left: 12px; color: #c0c4cc; font-size: 12px; }
.file-grid { display: flex; flex-wrap: wrap; gap: 6px; }
.fsize { color: #909399; margin-left: 4px; font-size: 11px; }
</style>
