/**
 * PVE 连接的加载、切换与增删改测。
 *
 * 从 `PvePanel.vue` 抽出的"连接"职责：连接列表加载、节点数汇总、
 * 连接 CRUD 弹窗、连通性测试。与"资源加载"(`loadAll`)解耦——后者通过
 * `refreshResources` 回调注入，连接切换/变更后由面板触发资源刷新。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref } from 'vue'

import { pveAPI } from '@/api'
import type { PveConnection } from '@/types/pve'

export function usePveConnections(refreshResources: (quiet?: boolean) => void | Promise<void>) {
  // ── 连接 ──
  const connections = ref<PveConnection[]>([])
  const connLoading = ref(false)
  const activeConnId = ref<number | null>(null)
  const enabledConnectionCount = computed(() => connections.value.filter((connection) => connection.enabled).length)
  const totalNodeCount = ref(0)
  let nodeSummaryRequest = 0

  async function loadTotalNodeCount(items = connections.value) {
    const requestId = ++nodeSummaryRequest
    const enabledConnections = items.filter((connection) => connection.enabled)
    if (!enabledConnections.length) {
      totalNodeCount.value = 0
      return
    }
    const results = await Promise.allSettled(enabledConnections.map((connection) => pveAPI.listNodes(connection.id)))
    if (requestId !== nodeSummaryRequest) return
    totalNodeCount.value = results.reduce(
      (total, result) => total + (result.status === 'fulfilled' ? result.value.data.length : 0),
      0,
    )
  }

  async function loadConnections() {
    connLoading.value = true
    try {
      const res = await pveAPI.listConnections()
      connections.value = res.data
      void loadTotalNodeCount(res.data)
      if (!activeConnId.value && res.data.length) {
        const first = res.data.find((c) => c.enabled) || res.data[0]
        activeConnId.value = first.id
        await refreshResources(true)
      }
    } catch {
      ElMessage.error('加载 PVE 连接失败')
    } finally {
      connLoading.value = false
    }
  }

  function onConnChange() {
    void refreshResources(true)
  }

  async function refreshAll() {
    await Promise.all([refreshResources(false), loadTotalNodeCount()])
  }

  // ── 连接管理 ──
  const connMgrVisible = ref(false)
  const connFormVisible = ref(false)
  const connSaving = ref(false)
  const connEditing = ref<PveConnection | null>(null)
  const testBusyId = ref<number | null>(null)
  const connForm = reactive({
    name: '',
    host: '',
    port: 8006,
    token_id: '',
    token_secret: '',
    verifySslBool: false,
    enabledBool: true,
    description: '',
  })

  // 表单若是从管理弹窗里开的,关闭后再回到管理弹窗(避免两个弹窗叠层)
  const returnToMgr = ref(false)

  function openConnForm(c: PveConnection | null) {
    returnToMgr.value = connMgrVisible.value
    connMgrVisible.value = false
    connEditing.value = c
    Object.assign(connForm, {
      name: c?.name || '',
      host: c?.host || '',
      port: c?.port || 8006,
      token_id: c?.token_id || '',
      token_secret: '',
      verifySslBool: c ? !!c.verify_ssl : false,
      enabledBool: c ? !!c.enabled : true,
      description: c?.description || '',
    })
    connFormVisible.value = true
  }

  function onConnFormClosed() {
    if (returnToMgr.value) {
      returnToMgr.value = false
      connMgrVisible.value = true
    }
  }

  async function saveConn() {
    if (!connForm.name.trim() || !connForm.host.trim() || !connForm.token_id.trim()) {
      ElMessage.warning('请填写名称、主机和 Token ID')
      return
    }
    if (!connEditing.value && !connForm.token_secret.trim()) {
      ElMessage.warning('请填写 Token Secret')
      return
    }
    connSaving.value = true
    const payload = {
      name: connForm.name,
      host: connForm.host,
      port: connForm.port,
      token_id: connForm.token_id,
      verify_ssl: connForm.verifySslBool ? 1 : 0,
      enabled: connForm.enabledBool ? 1 : 0,
      description: connForm.description,
    }
    try {
      if (connEditing.value) {
        await pveAPI.updateConnection(connEditing.value.id, {
          ...payload,
          token_secret: connForm.token_secret || undefined,
        })
        ElMessage.success('连接已更新')
      } else {
        await pveAPI.createConnection({ ...payload, token_secret: connForm.token_secret })
        ElMessage.success('连接已创建')
      }
      connFormVisible.value = false
      await loadConnections()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '保存失败')
    } finally {
      connSaving.value = false
    }
  }

  async function testConn(c: PveConnection) {
    testBusyId.value = c.id
    try {
      const res = await pveAPI.testConnection(c.id)
      ElMessage.success(`连接成功,PVE 版本 ${res.data.version || '未知'}`)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      ElMessage.error(err.response?.data?.detail || '连接失败')
    } finally {
      testBusyId.value = null
    }
  }

  async function removeConn(c: PveConnection) {
    try {
      await ElMessageBox.confirm(`确定删除连接「${c.name}」?`, '删除确认', { type: 'warning' })
      await pveAPI.deleteConnection(c.id)
      ElMessage.success('连接已删除')
      if (activeConnId.value === c.id) activeConnId.value = null
      await loadConnections()
    } catch {
      /* cancelled or error */
    }
  }

  return {
    connections,
    connLoading,
    activeConnId,
    enabledConnectionCount,
    totalNodeCount,
    loadConnections,
    onConnChange,
    refreshAll,
    connMgrVisible,
    connFormVisible,
    connSaving,
    connEditing,
    testBusyId,
    connForm,
    openConnForm,
    onConnFormClosed,
    saveConn,
    testConn,
    removeConn,
  }
}
