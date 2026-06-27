<template>
  <el-dialog v-model="visible" :title="path" width="80%" top="5vh" @open="load">
    <div v-loading="loading">
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <pre v-else class="file-content">{{ content }}</pre>
    </div>
  </el-dialog>
</template>

<script setup>
import { ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({
  projectId: { type: String, required: true },
  path: { type: String, default: '' },
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const visible = ref(props.modelValue)
watch(() => props.modelValue, v => { visible.value = v })
watch(visible, v => emit('update:modelValue', v))

const content = ref('')
const loading = ref(false)
const error = ref('')

async function load() {
  if (!props.path) return
  loading.value = true
  error.value = ''
  content.value = ''
  try {
    content.value = await api.getFile(props.projectId, props.path)
  } catch (e) {
    error.value = '读取失败：' + (e.response?.data?.detail || e.message)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.file-content {
  max-height: 70vh;
  overflow: auto;
  background: #fafafa;
  padding: 12px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
