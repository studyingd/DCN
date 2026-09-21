<!--
  FileManager.vue — remote file browser.

  传输后端由连接类型唯一决定，不做探测：
    · SSH  → SFTP（REST 后端，可浏览远程任意路径）
    · RDP  → GuacamoleFS 共享盘（guacd 随会话下发的虚拟盘）

  曾经 RDP 会先探测目标是否装了 OpenSSH，有则走 SFTP、无则回落共享盘并弹
  “未检测到 SFTP 服务”横幅。实际部署里 Windows 目标不开 OpenSSH，那条提示恒成立，
  只是白白多一次最长 15s 的探测和一个“降级”错觉，已整体移除。
-->

<template>
  <el-dialog
    :model-value="modelValue"
    title="文件管理"
    width="740px"
    :close-on-click-modal="false"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
    @open="onOpen"
  >
    <div class="fm-toolbar">
      <el-button size="small" :disabled="!canGoUp" @click="goUp">上级</el-button>
      <el-input
        v-model="pathInput"
        size="small"
        class="fm-path-input"
        :placeholder="isGuac ? '盘内路径，如 / 或 /子目录' : '绝对路径，如 /tmp、/opt'"
        :disabled="isGuac && !fsReady"
        @keyup.enter="gotoPath"
      />
      <el-button size="small" :disabled="isGuac && !fsReady" @click="gotoPath">跳转</el-button>
      <el-tag size="small" :type="modeTagType" class="fm-mode-tag">{{ modeLabel }}</el-tag>
      <div class="fm-spacer" />
      <el-button size="small" :loading="loading" :disabled="isGuac && !fsReady" @click="load(cwd)"> 刷新 </el-button>
      <el-upload
        :show-file-list="false"
        :auto-upload="true"
        :http-request="customUpload"
        :disabled="uploading || (isGuac && !fsReady)"
      >
        <el-button size="small" type="primary" :loading="uploading">
          {{ uploadLabel }}
        </el-button>
      </el-upload>
    </div>

    <div v-if="isGuac && !fsReady" class="fm-banner">
      共享盘尚未就绪：guacd 会在 RDP 会话建立后下发虚拟盘，请确认远程桌面已连接成功。
    </div>
    <div v-else-if="isGuac" class="fm-banner fm-banner-info">
      共享盘模式：这里是映射进远程桌面的 GuacamoleFS 目录，不是远程整机磁盘。要取回其它位置的文件，请先在 Windows
      里把它复制进该共享盘。
    </div>

    <template v-if="uploading">
      <el-progress
        :percentage="uploadPct"
        :stroke-width="8"
        :indeterminate="uploadPhase === 'flushing'"
        :duration="2"
        :show-text="uploadPhase === 'sending'"
        style="margin-bottom: 4px"
      />
      <div v-if="uploadPhase === 'flushing'" class="fm-flush-hint">
        {{
          isGuac
            ? '数据已交给 guacd，正在写入远程共享盘…（大文件会持续较久，请勿关闭窗口）'
            : '数据已上传到服务端，正在写入远端磁盘…（大文件会持续较久，请勿关闭窗口）'
        }}
      </div>
    </template>

    <el-table
      v-loading="loading"
      :data="entries"
      size="small"
      max-height="440"
      :empty-text="isGuac && !fsReady ? '等待共享盘就绪…' : '空目录'"
      @row-dblclick="onRowDblClick"
    >
      <el-table-column label="名称" min-width="260">
        <template #default="{ row }">
          <el-icon v-if="row.type === 'dir'" class="fm-ico fm-ico-dir"><Folder /></el-icon>
          <el-icon v-else class="fm-ico"><Document /></el-icon>
          <span class="fm-name" :class="{ 'fm-name-dir': row.type === 'dir' }">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="110">
        <template #default="{ row }">{{ row.type === 'dir' || !row.size ? '-' : formatSize(row.size) }}</template>
      </el-table-column>
      <el-table-column label="修改时间" width="170">
        <template #default="{ row }">{{ row.mtime ? formatTime(row.mtime) : '-' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" align="center">
        <template #default="{ row }">
          <el-button v-if="row.type === 'file'" size="small" link type="primary" @click.stop="download(row)"
            >下载</el-button
          >
          <span v-else class="fm-hint">双击进入</span>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { Folder, Document } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fileAPI, type FileEntry } from '@/api'

/** RDP file backend supplied by useGuacamole (GuacamoleFS object-stream API). */
interface RdpFileBackend {
  isReady: { value: boolean }
  list: (path: string) => Promise<FileEntry[]>
  upload: (
    file: File,
    destDir: string,
    onProgress?: (pct: number, phase: 'sending' | 'flushing') => void,
  ) => Promise<number>
  download: (path: string, filename: string) => Promise<number>
}

const props = defineProps<{
  modelValue: boolean
  connType?: 'ssh' | 'rdp'
  deviceId: number
  credentialId?: number | null
  username?: string
  password?: string
  rdpBackend?: RdpFileBackend
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
}>()

const entries = ref<FileEntry[]>([])
const cwd = ref<string>('.')
const loading = ref(false)
const uploading = ref(false)
const uploadPct = ref(0)
/**
 * sending  = 数据仍在传往远端，百分比可信
 * flushing = 数据已全部交出，正在等远端落盘（这段无法观测，用不确定动画）
 *
 * 之前只有一个百分比：浏览器把数据交给 guacd/后端就算 100%，而真正写进远程
 * 磁盘还在后面，于是进度条"一上传就 99%，然后在 99% 卡很久"。
 */
const uploadPhase = ref<'sending' | 'flushing'>('sending')

// 传输后端由连接类型唯一决定，不再做 SFTP 探测：
//   SSH → SFTP（没有 guacd 参与，只能走 REST/SFTP）
//   RDP → GuacamoleFS 共享盘（guacd 随会话下发的虚拟盘）
// 曾经 RDP 会先探测目标有没有装 OpenSSH，探测失败才回落到共享盘，并弹一条
// "未检测到 SFTP 服务" 的横幅。实际部署里 Windows 目标都不开 OpenSSH，那条
// 横幅恒成立，只是白白增加一次最长 15s 的探测和一个"降级"的错觉。
const homePath = ref('') // SFTP 家目录（共享盘模式不用）
const pathInput = ref('') // editable path box

const isGuac = computed(() => props.connType === 'rdp')

// GuacamoleFS readiness (RDP drive object exposed by guacd). Vue unwraps the
// ref's `.value` inside computed, so this stays reactive.
const fsReady = computed(() => !!props.rdpBackend?.isReady?.value)

const modeLabel = computed(() => (isGuac.value ? 'RDP · 共享盘' : 'SFTP · 任意路径'))
const modeTagType = computed<'success' | 'info'>(() => (isGuac.value ? 'info' : 'success'))

// Keep the path box in sync with the current location; it only re-syncs when
// cwd actually changes, so the user can edit freely between navigations.
watch(
  cwd,
  (v) => {
    pathInput.value = v
  },
  { immediate: true },
)

const canGoUp = computed(() => cwd.value !== '/' && cwd.value.length > 0)

function initialPath(): string {
  if (isGuac.value) return '/'
  return homePath.value || '.'
}

function joinPath(base: string, name: string): string {
  if (isGuac.value) {
    if (base === '/' || base === '') return '/' + name
    return base.endsWith('/') ? base + name : base + '/' + name
  }
  // sftp: posix join on an absolute base
  if (!base || base === '.') return name
  if (base.endsWith('/')) return base + name
  return base + '/' + name
}

function parentPath(p: string): string {
  if (isGuac.value) {
    if (!p || p === '/') return '/'
    const idx = p.lastIndexOf('/')
    return idx <= 0 ? '/' : p.slice(0, idx)
  }
  // sftp: posix dirname, clamp at '/' — allowed to go above the home directory
  if (!p || p === '/') return '/'
  const idx = p.lastIndexOf('/')
  return idx <= 0 ? '/' : p.slice(0, idx)
}

function onOpen() {
  load(initialPath())
}

// Once guacd exposes the GuacamoleFS drive, (re)load — the dialog may have
// opened before the filesystem object arrived. SFTP mode ignores this.
watch(fsReady, (ready) => {
  if (isGuac.value && ready && props.modelValue && !loading.value) {
    load(cwd.value === '.' ? '/' : cwd.value)
  }
})

async function load(path: string) {
  loading.value = true
  try {
    if (isGuac.value) {
      const backend = props.rdpBackend
      if (!backend) {
        entries.value = []
        return
      }
      const list = await backend.list(path)
      cwd.value = path || '/'
      entries.value = list
    } else {
      const res = await fileAPI.list(props.deviceId, {
        path,
        credential_id: props.credentialId,
        username: props.username,
        password: props.password,
      })
      cwd.value = res.data.path
      entries.value = res.data.entries
      if (!homePath.value) homePath.value = res.data.path
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '读取目录失败')
    entries.value = []
  } finally {
    loading.value = false
  }
}

function gotoPath() {
  const raw = pathInput.value.trim()
  if (!raw) return
  // Force a leading slash: absolute posix path for SFTP, root-relative for the drive.
  const p = raw.startsWith('/') ? raw : '/' + raw
  load(p)
}

function onRowDblClick(row: FileEntry) {
  if (row.type === 'dir') load(joinPath(cwd.value, row.name))
}

function goUp() {
  load(parentPath(cwd.value))
}

const uploadLabel = computed(() => {
  if (!uploading.value) return '上传到当前目录'
  return uploadPhase.value === 'flushing' ? '写入远程…' : `上传中 ${uploadPct.value}%`
})

async function customUpload(option: any) {
  uploading.value = true
  uploadPct.value = 0
  uploadPhase.value = 'sending'
  try {
    if (isGuac.value) {
      const backend = props.rdpBackend
      if (!backend) throw new Error('RDP 文件系统未就绪')
      await backend.upload(option.file, cwd.value, (p, phase) => {
        uploadPct.value = p
        uploadPhase.value = phase
      })
    } else {
      const fd = new FormData()
      fd.append('file', option.file)
      fd.append('dest_dir', cwd.value || '.')
      if (props.credentialId) fd.append('credential_id', String(props.credentialId))
      if (props.username) fd.append('username', props.username)
      if (props.password) fd.append('password', props.password)
      await fileAPI.upload(props.deviceId, fd, (p) => {
        // axios 的进度只覆盖「浏览器 → 后端」这一段；请求体发完后，后端还要
        // 建 SSH 连接并把文件写进远端，那段时间用 flushing 阶段如实表达。
        if (p >= 100) {
          uploadPhase.value = 'flushing'
          uploadPct.value = 100
        } else {
          uploadPhase.value = 'sending'
          uploadPct.value = Math.min(99, p)
        }
      })
    }
    ElMessage.success(`已上传: ${option.file.name}`)
    await load(cwd.value)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '上传失败')
  } finally {
    uploading.value = false
    uploadPhase.value = 'sending'
    uploadPct.value = 0
  }
}

async function download(row: FileEntry) {
  const fullPath = joinPath(cwd.value, row.name)
  try {
    if (isGuac.value) {
      const backend = props.rdpBackend
      if (!backend) throw new Error('RDP 文件系统未就绪')
      await backend.download(fullPath, row.name)
    } else {
      const res = await fileAPI.download(props.deviceId, {
        path: fullPath,
        credential_id: props.credentialId,
        username: props.username,
        password: props.password,
      })
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      a.download = row.name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '下载失败')
  }
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1048576).toFixed(1) + ' MB'
}
function formatTime(unix: number): string {
  try {
    return new Date(unix * 1000).toLocaleString('zh-CN')
  } catch {
    return '-'
  }
}
</script>

<style scoped>
.fm-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.fm-path-input {
  flex: 1;
  min-width: 0;
  font-family: var(--dcn-font-mono, monospace);
}
.fm-path-input :deep(.el-input__inner) {
  font-size: 13px;
}
.fm-mode-tag {
  flex-shrink: 0;
}
.fm-banner {
  margin-bottom: 12px;
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--dcn-warning);
  background: var(--dcn-warning-bg);
  border: 1px solid var(--dcn-warning);
  border-radius: var(--dcn-radius-md, 6px);
}

/* 共享盘模式说明是常态信息，不是降级警告，用中性色。 */
.fm-flush-hint {
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--dcn-text-secondary);
}

.fm-banner-info {
  color: var(--dcn-text-secondary);
  background: var(--dcn-bg-muted);
  border-color: var(--dcn-border);
}
.fm-spacer {
  flex: 1;
}
.fm-ico {
  margin-right: 6px;
  vertical-align: middle;
}
.fm-ico-dir {
  color: var(--dcn-warning);
}
.fm-name {
  vertical-align: middle;
}
.fm-name-dir {
  font-weight: 600;
  cursor: pointer;
}
.fm-hint {
  color: var(--dcn-text-secondary, #909399);
  font-size: 12px;
}
</style>
