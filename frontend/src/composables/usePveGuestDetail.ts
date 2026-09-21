/**
 * PVE 虚拟机详情抽屉：运维接入绑定、历史指标、快照管理。
 *
 * 从 `PvePanel.vue` 抽出的"详情"职责。打开某台虚机后，这个 composable
 * 负责它自己的详情轮询、绑定 CRUD、OS 检测、WinRM 脚本下载、历史图表
 * 数据与快照增删回滚。对外的耦合点只有三个：
 *  - `activeConnId`：当前选中的 PVE 连接（来自 `usePveConnections`）
 *  - `refreshResources`：资源列表刷新（快照回滚等操作后触发）
 *  - `power`：电源操作仍由面板编排（它会反过来调 `refreshDetail`），
 *    详情模板里通过返回的 `power` 透传调用。
 *
 * 定时器生命周期：`openDetail` 启动详情轮询，`onDetailClosed`/`dispose`
 * 统一清理 `detailLoadTimer`/`detailPollTimer`，避免泄漏或后台请求抢占资源。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, type Ref } from 'vue'

import { pveAPI } from '@/api'
import type { PveGuest, PveGuestBinding, PveRrdPoint, PveSnapshot } from '@/types/pve'
import { extractErrorDetail, readHeader } from '@/utils/apiError'
import { validateSnapName } from '@/utils/pveFormat'

export function usePveGuestDetail(
  activeConnId: Ref<number | null>,
  refreshResources: (quiet?: boolean) => void | Promise<void>,
) {
  const detailVisible = ref(false)
  const detailLoading = ref(false)
  const detail = ref<PveGuest | null>(null)
  const guestBinding = ref<PveGuestBinding | null>(null)
  const bindingLoading = ref(false)
  const bindingVisible = ref(false)
  const bindingSaving = ref(false)
  const bindingTestLoading = ref(false)
  const winrmScriptLoading = ref(false)
  const bindingOsDetecting = ref(false)
  const bindingOsDetection = ref<{ os_system: string; winrm_port_open: boolean; detail: string } | null>(null)
  const bindingForm = reactive({
    ip_address: '',
    os_system: 'unknown' as 'linux' | 'windows' | 'unknown',
    ssh_port: 22,
    winrm_port: 5985,
    username: '',
    password: '',
    ssh_key: '',
    enabled: true,
  })
  const snapshots = ref<PveSnapshot[]>([])
  const historyRange = ref('1h')
  const historyPoints = ref<PveRrdPoint[]>([])
  const historyLoading = ref(false)
  const historyError = ref('')
  let historyRequest = 0
  let detailLoadTimer: number | undefined
  let detailPollTimer: number | undefined
  let detailRefreshInFlight = false
  let detailRefreshQueued = false
  // 后端补探精确 OS 名(_maybe_backfill_os_name)是后台线程,延迟重拉显示结果
  let bindingReloadTimer: number | undefined
  let bindingReloadTries = 0

  // ── 快照 ──
  const snapName = ref('')
  const snapBusy = ref(false)

  // ── 调整配置(CPU/内存/磁盘扩容) ──
  const configVisible = ref(false)
  const configSaving = ref(false)
  const configForm = reactive({
    cores: 2,
    memory_mb: 2048,
    disk_gb: 32,
    /** 当前磁盘容量(字节),从 detail.maxdisk 取;提交时用于「只扩不缩」校验 */
    currentDiskBytes: 0,
    /** 打开弹窗时的初始值,用于「当前 → 新值」对比与变更摘要 */
    currentCores: 2,
    currentMemoryMb: 2048,
  })

  /** 三项各自的变更判定(与初始值比较)。 */
  const configChanged = computed(() => ({
    cores: configForm.cores !== configForm.currentCores,
    memory: configForm.memory_mb !== configForm.currentMemoryMb,
    disk: configForm.disk_gb * 1024 ** 3 > configForm.currentDiskBytes,
  }))
  const hasConfigChange = computed(
    () => configChanged.value.cores || configChanged.value.memory || configChanged.value.disk,
  )

  async function openDetail(g: PveGuest) {
    if (detailLoadTimer) window.clearTimeout(detailLoadTimer)
    detail.value = g
    detailVisible.value = true
    historyRange.value = '1h'
    snapName.value = ''
    guestBinding.value = null
    void refreshDetail()
    if (detailPollTimer) window.clearInterval(detailPollTimer)
    detailPollTimer = window.setInterval(() => {
      if (!document.hidden && detailVisible.value && !bindingVisible.value) {
        void refreshDetail(false)
      }
    }, 30000)
    bindingReloadTries = 0
    // 先加载运维绑定，让配置入口尽快可用；历史和快照放到下一帧后后台加载。
    void loadGuestBinding()
    detailLoadTimer = window.setTimeout(() => {
      if (detailVisible.value && detail.value?.vmid === g.vmid && detail.value?.type === g.type) {
        // 用户已经进入配置弹窗时继续延后，避免配置输入与图表/快照请求争抢资源。
        if (bindingVisible.value) {
          detailLoadTimer = window.setTimeout(() => {
            if (detailVisible.value && !bindingVisible.value) {
              void loadSnapshots()
              void loadHistory()
            }
          }, 500)
        } else {
          void loadSnapshots()
          void loadHistory()
        }
      }
    }, 120)
  }

  function onDetailClosed() {
    if (detailLoadTimer) window.clearTimeout(detailLoadTimer)
    detailLoadTimer = undefined
    if (bindingReloadTimer) window.clearTimeout(bindingReloadTimer)
    bindingReloadTimer = undefined
    if (detailPollTimer) window.clearInterval(detailPollTimer)
    detailPollTimer = undefined
    detailRefreshQueued = false
    // 使尚未返回的历史请求失效，避免关闭详情后又触发无意义的响应式更新。
    historyRequest += 1
  }

  /** 组件卸载时的兜底清理（onUnmounted 调用）。 */
  function dispose() {
    if (detailLoadTimer) window.clearTimeout(detailLoadTimer)
    if (detailPollTimer) window.clearInterval(detailPollTimer)
  }

  async function loadGuestBinding() {
    if (!detail.value || !activeConnId.value) return
    bindingLoading.value = true
    try {
      const res = await pveAPI.guestBinding(activeConnId.value, detail.value.type, detail.value.vmid, detail.value.node)
      guestBinding.value = res.data
      // 后端发现有凭据但缺精确 OS 名时会触发后台补探(SSH os-release /
      // WinRM Caption),几秒后重拉一次让「系统」列显示版本号;最多 3 次。
      if (res.data && !res.data.os_name && res.data.configured && bindingReloadTries < 3) {
        bindingReloadTries += 1
        if (bindingReloadTimer) window.clearTimeout(bindingReloadTimer)
        bindingReloadTimer = window.setTimeout(() => {
          if (detailVisible.value) void loadGuestBinding()
        }, 8000)
      }
    } catch {
      guestBinding.value = null
    } finally {
      bindingLoading.value = false
    }
  }

  async function openBindingForm() {
    // 打开前先确保 binding 数据已加载——后端返回时已用 QGA → PVE ostype → 历史 binding
    // 推断出 os_system,这里直接采用,用户不需要也不应该手选操作系统。
    if (!guestBinding.value && !bindingLoading.value) {
      await loadGuestBinding()
    }
    const binding = guestBinding.value
    Object.assign(bindingForm, {
      ip_address: binding?.ip_address || binding?.qga_ip_address || '',
      // 后端 _guest_binding_view 返回的 os_system 已是 QGA → PVE ostype → 历史 binding
      // 推断完毕的值,直接用。LXC 一定是 linux,作为最后的兜底。
      os_system: binding?.os_system || 'unknown',
      ssh_port: binding?.ssh_port || 22,
      winrm_port: binding?.winrm_port || 5985,
      username: binding?.username || '',
      password: '',
      ssh_key: '',
      enabled: binding?.enabled ?? true,
    })
    bindingOsDetection.value = null
    bindingVisible.value = true
    // 后端未确证(os_system 为 unknown 或排除法假设的 linux)时,打开窗口即自动
    // 做一次免凭据探测(SSH banner + 22/3389/5985/445 端口指纹),纠正藏着的 Windows。
    const osConfirmed =
      !binding?.os_assumed && (bindingForm.os_system === 'linux' || bindingForm.os_system === 'windows')
    if (!osConfirmed && bindingForm.ip_address.trim()) {
      void detectBindingOs(true)
    }
  }

  async function detectBindingOs(silent = false) {
    if (!detail.value || !activeConnId.value || !bindingForm.ip_address.trim()) return
    bindingOsDetecting.value = true
    try {
      const res = await pveAPI.detectGuestBindingOs(activeConnId.value, detail.value.type, detail.value.vmid, {
        ip_address: bindingForm.ip_address.trim(),
        username: bindingForm.username.trim() || undefined,
        password: bindingForm.password || undefined,
        winrm_port: bindingForm.winrm_port,
      })
      bindingOsDetection.value = res.data
      if (res.data.os_system === 'windows' || res.data.os_system === 'linux') {
        bindingForm.os_system = res.data.os_system as 'linux' | 'windows'
      }
      if (!silent) {
        ElMessage.success(
          `检测结果：${res.data.os_system === 'windows' ? 'Windows' : res.data.os_system === 'linux' ? 'Linux' : '未识别'}`,
        )
      }
    } catch (e: unknown) {
      // silent 模式(打开对话框/保存前的自动探测)不弹错误——IP 不可达是常态,
      // 探测结果区(bindingOsDetection)和「未识别」标签本身已是足够反馈。
      if (!silent) {
        const err = e as { response?: { data?: { detail?: string } } }
        ElMessage.error(err.response?.data?.detail || '操作系统检测失败')
      }
    } finally {
      bindingOsDetecting.value = false
    }
  }

  async function saveGuestBinding() {
    if (
      !detail.value ||
      !activeConnId.value ||
      (!bindingForm.ip_address.trim() && !guestBinding.value?.qga_available) ||
      !bindingForm.username.trim()
    )
      return
    // 保存前对未确证的 OS 带凭据再静默探测一次:既纠正排除法假设里藏着的
    // Windows,后端也会把精确版本名写回 binding.os_name。
    const osConfirmed =
      !guestBinding.value?.os_assumed && (bindingForm.os_system === 'linux' || bindingForm.os_system === 'windows')
    if (!osConfirmed && bindingForm.ip_address.trim()) {
      await detectBindingOs(true)
    }
    bindingSaving.value = true
    try {
      const res = await pveAPI.updateGuestBinding(
        activeConnId.value,
        detail.value.type,
        detail.value.vmid,
        {
          ...bindingForm,
          ip_address: bindingForm.ip_address.trim() || undefined,
        },
        detail.value.node,
      )
      guestBinding.value = res.data
      bindingVisible.value = false
      ElMessage.success('虚拟机运维接入已保存')
      // 精确版本号不依赖 QGA:保存后凭据已落库,后台再静默探测一次,
      // 后端经 SSH /etc/os-release 或 WinRM Caption 拿精确名并写回
      // binding.os_name(保存前探测时 binding 可能还不存在,无处落库),
      // 完成后刷新绑定让「系统」列立即显示。
      if (!res.data.os_name && res.data.ip_address) {
        void detectBindingOs(true).finally(() => loadGuestBinding())
      }
      // The detail endpoint now performs QGA-first filesystem collection and
      // falls back to the newly saved SSH/WinRM binding when needed.
      void refreshDetail()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '保存运维接入失败')
    } finally {
      bindingSaving.value = false
    }
  }

  async function testGuestAccess() {
    if (!detail.value || !activeConnId.value) return
    bindingTestLoading.value = true
    try {
      await pveAPI.testGuestBinding(activeConnId.value, detail.value.type, detail.value.vmid)
      ElMessage.success('虚拟机运维连接测试成功')
      await loadGuestBinding()
      void refreshDetail()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '虚拟机运维连接测试失败')
      await loadGuestBinding()
    } finally {
      bindingTestLoading.value = false
    }
  }

  async function downloadGuestWinrmScript() {
    if (!detail.value || !activeConnId.value || !guestBinding.value?.ip_address) return
    winrmScriptLoading.value = true
    try {
      const res = await pveAPI.downloadGuestWinrmSetupScript(activeConnId.value, detail.value.type, detail.value.vmid)
      const url = URL.createObjectURL(res.data)
      const link = document.createElement('a')
      link.href = url
      link.download = 'dcn-enable-winrm.ps1'
      link.click()
      URL.revokeObjectURL(url)
      // 脚本里的防火墙规则只放行这个出口 IP。Docker 部署时它可能是容器内网地址
      // （172.17.x.x），与虚拟机实际看到的来源不一致，所以显式告知便于核对。
      const sourceIp = readHeader(res.headers, 'X-DCN-WinRM-Source-IP')
      ElMessage.success(sourceIp ? `WinRM 启用脚本已下载（仅放行 DCN 出口 IP ${sourceIp}）` : 'WinRM 启用脚本已下载')
    } catch (error) {
      // 不能笼统说「请配置有效的虚拟机 IP」：IP 往往已经由 QGA 拿到了，
      // 真正的原因（IPv6 目标 / 无法确定出口 IP / 路由不通）在后端 detail 里。
      // 注意 responseType:'blob' 时错误体是 Blob，必须读成文本再解析。
      ElMessage.error(await extractErrorDetail(error, '脚本生成失败'))
    } finally {
      winrmScriptLoading.value = false
    }
  }

  async function loadHistory() {
    if (!detail.value || !activeConnId.value) return
    const requestId = ++historyRequest
    const target = {
      connId: activeConnId.value,
      type: detail.value.type,
      vmid: detail.value.vmid,
    }
    historyLoading.value = true
    historyError.value = ''
    try {
      const res = await pveAPI.guestHistory(
        target.connId,
        target.type,
        target.vmid,
        historyRange.value,
        detail.value.node,
      )
      if (
        requestId !== historyRequest ||
        !detail.value ||
        detail.value.type !== target.type ||
        detail.value.vmid !== target.vmid ||
        activeConnId.value !== target.connId
      )
        return
      const points = res.data.items || []
      historyPoints.value = points
      // PVE RRD 的最新采样本身就是速率，打开详情时直接填充，避免等待下一轮 10 秒轮询。
      const latest = [...points].reverse().find((point) => Number.isFinite(Number(point.time)))
      if (latest) {
        const latestRate = (value: unknown, fallback: number | undefined) =>
          value === null || value === undefined || !Number.isFinite(Number(value)) ? fallback : Number(value)
        detail.value = {
          ...detail.value,
          disk_read_rate: latestRate(latest.diskread, detail.value.disk_read_rate),
          disk_write_rate: latestRate(latest.diskwrite, detail.value.disk_write_rate),
          net_in_rate: latestRate(latest.netin, detail.value.net_in_rate),
          net_out_rate: latestRate(latest.netout, detail.value.net_out_rate),
        }
      }
    } catch {
      if (requestId !== historyRequest) return
      historyPoints.value = []
      historyError.value = '历史数据加载失败，请检查 PVE RRD 数据和 Token 权限'
    } finally {
      if (requestId === historyRequest) historyLoading.value = false
    }
  }

  async function refreshDetail(loadSnapshot = true) {
    if (!detail.value || !activeConnId.value) return
    if (detailRefreshInFlight) {
      detailRefreshQueued = true
      return
    }
    const target = {
      connId: activeConnId.value,
      type: detail.value.type,
      vmid: detail.value.vmid,
    }
    detailRefreshInFlight = true
    try {
      const res = await pveAPI.guestDetail(target.connId, target.type, target.vmid)
      if (
        !detail.value ||
        !detailVisible.value ||
        activeConnId.value !== target.connId ||
        detail.value.type !== target.type ||
        detail.value.vmid !== target.vmid
      )
        return
      const st = res.data.status as Record<string, unknown>
      detail.value = { ...detail.value, ...st, guest_agent: res.data.guest_agent } as PveGuest
    } catch {
      /* ignore */
    } finally {
      detailRefreshInFlight = false
    }
    if (loadSnapshot) loadSnapshots()
    if (detailRefreshQueued && detailVisible.value) {
      detailRefreshQueued = false
      void refreshDetail(false)
    }
  }

  async function loadSnapshots() {
    if (!detail.value || !activeConnId.value) return
    try {
      const res = await pveAPI.snapshots(activeConnId.value, detail.value.type, detail.value.vmid, detail.value.node)
      snapshots.value = res.data
    } catch {
      snapshots.value = []
    }
  }

  // ── 调整配置 ──
  function openConfigForm() {
    if (!detail.value) return
    const diskBytes = Number(detail.value.maxdisk) || 0
    const cores = Number(detail.value.cpus || detail.value.maxcpu) || 2
    const memoryMb = Math.round((Number(detail.value.maxmem) || 2048 * 1024 ** 2) / 1024 ** 2)
    Object.assign(configForm, {
      cores,
      memory_mb: memoryMb,
      disk_gb: Math.max(1, Math.round(diskBytes / 1024 ** 3)),
      currentDiskBytes: diskBytes,
      currentCores: cores,
      currentMemoryMb: memoryMb,
    })
    configVisible.value = true
  }

  async function saveGuestConfig() {
    if (!detail.value || !activeConnId.value) return
    configSaving.value = true
    try {
      const res = await pveAPI.updateGuestConfig(
        activeConnId.value,
        detail.value.type,
        detail.value.vmid,
        {
          cores: configForm.cores,
          memory_mb: configForm.memory_mb,
          // 与当前容量相同则不提交(后端会拒绝不大于当前的值)
          disk_gb: configForm.disk_gb * 1024 ** 3 > configForm.currentDiskBytes ? configForm.disk_gb : undefined,
        },
        detail.value.node,
      )
      configVisible.value = false
      ElMessage.success(res.data.message || '配置已更新')
      // 刷新详情与列表(config/status 已在响应里,但详情还有 QGA/快照等聚合,整拉最稳)
      void refreshDetail()
      void loadGuestBinding()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '调整配置失败')
    } finally {
      configSaving.value = false
    }
  }

  // ── 快照操作 ──
  async function createSnap() {
    if (!detail.value) return
    // 快照名提前校验(PVE 只接受字母/数字/下划线,current 保留):非法名只会
    // 拿到 PVE 原始 400,提前拦下并给中文说明。
    const nameError = validateSnapName(snapName.value)
    if (nameError) {
      ElMessage.warning(nameError)
      return
    }
    snapBusy.value = true
    try {
      await pveAPI.createSnapshot(activeConnId.value!, detail.value.type, detail.value.vmid, snapName.value.trim(), '')
      // 快照创建是异步任务(运行中虚机带内存快照可能要几秒到几十秒),
      // 这里只是“已提交”;列表刷新后新快照才会出现。
      ElMessage.success(`已提交快照「${snapName.value}」创建任务`)
      snapName.value = ''
      loadSnapshots()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '创建快照失败')
    } finally {
      snapBusy.value = false
    }
  }

  async function rollbackSnap(s: PveSnapshot) {
    if (!detail.value) return
    try {
      await ElMessageBox.confirm(`确定回滚到快照「${s.name}」?当前状态会丢失。`, '回滚确认', { type: 'warning' })
      await pveAPI.rollbackSnapshot(activeConnId.value!, detail.value.type, detail.value.vmid, s.name)
      ElMessage.success(`已回滚到「${s.name}」`)
      setTimeout(() => refreshResources(true), 2000)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      if ((e as { message?: string })?.message !== 'cancel') {
        ElMessage.error(err.response?.data?.detail || '回滚失败')
      }
    }
  }

  async function deleteSnap(s: PveSnapshot) {
    if (!detail.value) return
    try {
      await ElMessageBox.confirm(`确定删除快照「${s.name}」?`, '删除确认', { type: 'warning' })
      await pveAPI.deleteSnapshot(activeConnId.value!, detail.value.type, detail.value.vmid, s.name)
      ElMessage.success('快照已删除')
      loadSnapshots()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      if ((e as { message?: string })?.message !== 'cancel') {
        ElMessage.error(err.response?.data?.detail || '删除失败')
      }
    }
  }

  return {
    detailVisible,
    detailLoading,
    detail,
    guestBinding,
    bindingLoading,
    bindingVisible,
    bindingSaving,
    bindingTestLoading,
    winrmScriptLoading,
    bindingOsDetecting,
    bindingOsDetection,
    bindingForm,
    snapshots,
    historyRange,
    historyPoints,
    historyLoading,
    historyError,
    snapName,
    snapBusy,
    configVisible,
    configSaving,
    configForm,
    configChanged,
    hasConfigChange,
    openConfigForm,
    saveGuestConfig,
    openDetail,
    onDetailClosed,
    dispose,
    loadGuestBinding,
    openBindingForm,
    detectBindingOs,
    saveGuestBinding,
    testGuestAccess,
    downloadGuestWinrmScript,
    loadHistory,
    refreshDetail,
    loadSnapshots,
    createSnap,
    rollbackSnap,
    deleteSnap,
  }
}
