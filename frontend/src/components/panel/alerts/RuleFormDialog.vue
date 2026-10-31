<template>
  <el-dialog
    v-model="formVisible"
    :title="editing ? '编辑告警规则' : '新建告警规则'"
    width="720px"
    class="rule-dialog"
    :close-on-click-modal="false"
  >
    <el-form ref="formRef" :model="form" :rules="formRules" label-position="top">
      <section class="rule-section identity-section">
        <div class="rule-section-title">
          <span>01</span>
          <div><strong>基础信息</strong><small>设置规则名称和需要监控的对象类型</small></div>
        </div>
        <el-form-item label="规则名称" prop="name"
          ><el-input v-model.trim="form.name" placeholder="例如：生产环境主机状态告警"
        /></el-form-item>
        <div class="type-card-grid">
          <button
            v-for="item in categoryOptions"
            :key="item.value"
            type="button"
            class="type-card"
            :class="{ active: form.category === item.value }"
            :aria-pressed="form.category === item.value"
            @click="selectCategory(item.value)"
          >
            <span class="type-card-icon"
              ><el-icon><component :is="item.icon" /></el-icon></span
            ><span class="type-card-copy"
              ><strong>{{ item.label }}</strong
              ><small>{{ item.description }}</small></span
            ><span class="type-card-check">✓</span>
          </button>
        </div>
      </section>
      <section class="rule-section">
        <div class="rule-section-title">
          <span>02</span>
          <div>
            <strong>触发策略</strong
            ><small>{{
              form.category === 'performance' ? '定义指标阈值、持续和重复通知时间' : '状态类规则仅配置持续和冷却时间'
            }}</small>
          </div>
        </div>
        <div v-if="form.category === 'performance'" class="form-grid">
          <el-form-item label="监控指标" prop="metric"
            ><el-select v-model="form.metric" style="width: 100%" @change="onMetricChange"
              ><el-option label="CPU 使用率" value="cpu_pct" /><el-option
                label="内存使用率"
                value="mem_pct" /><el-option label="磁盘使用率" value="disk_max_pct" /></el-select></el-form-item
          ><el-form-item label="告警级别" prop="severity"
            ><el-select v-model="form.severity" style="width: 100%"
              ><el-option label="信息" value="info" /><el-option label="警告" value="warning" /><el-option
                label="严重"
                value="critical" /></el-select
          ></el-form-item>
        </div>
        <!-- 状态类规则(主机/容器/业务)不显示告警级别(2026-09-18 用户定调):
              一律严重,保存时固定 critical(见 submitRule);级别选择只在数值型规则上保留 -->
        <div class="policy-grid" :class="{ 'state-policy-grid': form.category !== 'performance' }">
          <el-form-item v-if="isNumericMetric" label="触发阈值" prop="threshold"
            ><el-input-number
              v-model="form.threshold"
              :controls="false"
              :min="0"
              :max="100"
              :precision="1"
              style="width: 100%"
            /><span class="input-suffix">%</span></el-form-item
          ><el-form-item label="持续时长" prop="sustain_seconds"
            ><el-input-number
              v-model="form.sustain_seconds"
              :controls="false"
              :min="0"
              :max="86400"
              style="width: 100%"
            /><span class="input-suffix">秒</span></el-form-item
          ><el-form-item label="冷却时间" prop="cooldown_seconds"
            ><el-input-number
              v-model="form.cooldown_seconds"
              :controls="false"
              :min="30"
              :max="86400"
              style="width: 100%"
            /><span class="input-suffix">秒</span></el-form-item
          >
        </div>
        <div class="policy-hint">
          异常持续 {{ form.sustain_seconds }} 秒后触发；持续异常时每 {{ form.cooldown_seconds }} 秒最多重复通知一次。
        </div>
      </section>
      <section class="rule-section target-section">
        <div class="rule-section-title">
          <span>03</span>
          <div>
            <strong>监控范围</strong><small>{{ isContainerMetric ? containerScopeHint : scopeSectionHint }}</small>
          </div>
        </div>
        <!-- 与角色管理同一交互:全部资源 / 指定资源;空目标列表在服务端即代表「全部」 -->
        <el-form-item class="target-form-item">
          <div class="target-scope">
            <el-radio-group v-model="form.target_scope" @change="onTargetScopeChange">
              <el-radio value="all">{{ scopeAllLabel }}</el-radio>
              <el-radio value="selected">{{ scopeSelectedLabel }}</el-radio>
            </el-radio-group>
            <template v-if="form.target_scope === 'selected'">
              <el-button v-if="usesDeviceSelector" size="small" @click="targetSelectorVisible = true">
                {{ scopeButtonText }}
              </el-button>
              <el-select
                v-if="isBusinessMetric"
                v-model="form.target_business_ids"
                multiple
                clearable
                filterable
                collapse-tags
                :placeholder="`选择要监控的${scopeNoun}`"
                class="target-business-select"
                ><el-option
                  v-for="item in businesses"
                  :key="item.business_id"
                  :label="item.name"
                  :value="item.business_id"
              /></el-select>
            </template>
          </div>
        </el-form-item>
        <div v-if="form.target_scope === 'selected'" class="scope-tip">
          <template v-if="isContainerMetric"
            >仅对勾选的容器触发告警；设备分组标题上的全选可一次勾选该设备全部容器；不勾选任何容器则不会告警。</template
          ><template v-else-if="isBusinessMetric">仅对勾选的{{ scopeNoun }}触发告警；留空则不会告警。</template
          ><template v-else>仅对勾选的设备 / 虚拟机触发告警；不勾选任何对象则不会告警。</template>
        </div>
      </section>
      <div class="rule-enable">
        <div><strong>启用告警规则</strong><span>保存后从下一轮数据采集开始评估</span></div>
        <el-switch v-model="form.enabled" />
      </div>
    </el-form>
    <template #footer
      ><el-button @click="formVisible = false">取消</el-button
      ><el-button type="primary" :loading="saving" @click="submitRule">保存规则</el-button></template
    >
  </el-dialog>
  <!-- 监控对象选择:与角色管理同一交互(机房/虚拟化侧栏 + 卡片),append-to-body 嵌套在规则弹窗之上 -->
  <el-dialog
    v-model="targetSelectorVisible"
    :title="isContainerMetric ? '选择监控容器' : '选择监控对象'"
    width="860px"
    append-to-body
    class="rule-dialog target-dialog"
    :close-on-click-modal="false"
  >
    <!-- 性能指标 / 主机状态:设备 + 虚拟机卡片选择 -->
    <el-alert
      v-if="form.metric === 'disk_max_pct'"
      type="info"
      :closable="false"
      show-icon
      title="磁盘使用率:虚拟机需配置运维接入(QGA 自动采集优先,SSH/WinRM 兜底)才可勾选"
      style="margin-bottom: 10px"
    />
    <DeviceSelector
      v-if="!isContainerMetric"
      ref="deviceSelectorRef"
      :selected-ids="form.target_device_ids"
      :selectable-fn="targetSelectable"
      @confirm="onDeviceSelectorConfirm"
    />
    <!-- 容器状态:直接按设备分组勾选容器,无需先选设备 -->
    <div v-else class="container-picker">
      <div class="container-picker-toolbar">
        <el-input
          v-model="containerSearch"
          clearable
          placeholder="搜索容器或所属设备"
          :prefix-icon="Search"
          class="container-picker-search"
        />
        <el-checkbox
          v-model="containerAllChecked"
          :indeterminate="containerAllIndeterminate"
          @change="onContainerAllChange"
          >全选（{{ containerTotalCount }} 个容器）</el-checkbox
        >
        <span class="container-toolbar-side"
          ><el-tag size="small" effect="plain">已选 {{ form.target_container_ids.length }} 个</el-tag
          ><el-button
            v-if="form.target_container_ids.length"
            link
            type="primary"
            @click="form.target_container_ids = []"
            >清空</el-button
          ></span
        >
      </div>
      <div class="container-groups">
        <div v-for="group in filteredContainerGroups" :key="group.target_id" class="container-group">
          <div class="container-group-head">
            <el-checkbox
              :model-value="isGroupAllChecked(group)"
              :indeterminate="isGroupIndeterminate(group)"
              @change="(val: boolean) => toggleContainerGroup(group, val)"
              ><span class="container-group-name"
                ><el-icon><Monitor /></el-icon>{{ group.device_name
                }}<el-tag v-if="group.host_kind === 'pve_guest'" size="small" effect="plain" class="host-kind-tag"
                  >虚拟机</el-tag
                ></span
              ></el-checkbox
            ><span class="container-group-count">已选 {{ groupCheckedCount(group) }} / {{ group.items.length }}</span>
          </div>
          <div class="container-cards">
            <div
              v-for="item in group.items"
              :key="item.container_id"
              class="container-card"
              :class="{ checked: isContainerChecked(item) }"
              @click="toggleContainer(item)"
            >
              <el-checkbox :model-value="isContainerChecked(item)" @click.stop @change="() => toggleContainer(item)" />
              <span class="container-state-dot" :class="containerStateType(item)"></span>
              <div class="container-card-info">
                <div class="container-card-name">{{ item.name }}</div>
                <div class="container-card-meta">
                  <span>{{ item.state || 'unknown' }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-if="!filteredContainerGroups.length" description="没有匹配的容器" :image-size="48" />
      </div>
    </div>
    <template #footer>
      <el-button type="primary" @click="targetSelectorVisible = false">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { Box, Briefcase, Monitor, OfficeBuilding, Search } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { alertAPI } from '@/api'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import type { AlertRule, AlertSeverity } from '@/types'
import { errText, numericMetrics } from './alertShared'

const emit = defineEmits<{ (e: 'saved'): void }>()

const saving = ref(false)
const formVisible = ref(false)
const editing = ref<AlertRule | null>(null)
const formRef = ref<FormInstance>()
const businesses = ref<any[]>([])
const containers = ref<any[]>([])

const categoryOptions = [
  { value: 'performance', label: '性能指标', description: 'CPU、内存、磁盘使用率', icon: Monitor },
  { value: 'host', label: '主机状态', description: '离线或无法连接', icon: OfficeBuilding },
  { value: 'container', label: '容器状态', description: '退出、Restarting 或未运行', icon: Box },
  { value: 'business', label: '业务监控', description: '服务器或接口异常', icon: Briefcase },
]
const form = reactive<any>({
  name: '',
  category: 'performance',
  metric: 'cpu_pct',
  operator: 'gt',
  threshold: 90,
  severity: 'warning' as AlertSeverity,
  /** 监控范围:all = 全部资源(目标列表为空),selected = 指定资源;与角色管理同一口径 */
  target_scope: 'all' as 'all' | 'selected',
  target_device_ids: [] as number[],
  target_container_ids: [] as string[],
  target_business_ids: [] as number[],
  sustain_seconds: 60,
  cooldown_seconds: 300,
  enabled: true,
})
const formRules: FormRules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  threshold: [{ required: true, message: '请输入阈值', trigger: 'change' }],
}

const isNumericMetric = computed(() => numericMetrics.includes(form.metric))
const isDeviceMetric = computed(() =>
  ['cpu_pct', 'mem_pct', 'disk_max_pct', 'host_status', 'host_online'].includes(form.metric),
)
const isContainerMetric = computed(() =>
  ['container_status', 'container_running', 'container_restarting'].includes(form.metric),
)
// business_ 前缀判定：business_status 是唯一实际评估的指标，
// business_health/server_online/interface_up 是从未发射的死键(已从后端标签表
// 清理)；前缀匹配兼容历史遗留规则行，避免老规则丢失业务范围 UI。
const isBusinessMetric = computed(() => form.metric.startsWith('business_'))
/** 设备类与容器类规则共用「指定资源」选择对话框。 */
const usesDeviceSelector = computed(() => isDeviceMetric.value || isContainerMetric.value)
const hostSelectedCount = computed(() => form.target_device_ids.length)
/** 范围文案按规则类型变化(业务监控的对象是业务而不是资源)。 */
const scopeNoun = computed(() => (form.category === 'business' ? '业务' : '资源'))
const scopeAllLabel = computed(() => `全部${scopeNoun.value}`)
const scopeSelectedLabel = computed(() => `指定${scopeNoun.value}`)
const scopeSectionHint = computed(
  () =>
    `选择「全部${scopeNoun.value}」监控该类型下的所有对象；选择「指定${scopeNoun.value}」后从机房与虚拟化平台中勾选`,
)
const containerScopeHint = '选择「全部资源」监控所有设备上的容器；选择「指定资源」后按设备分组直接勾选容器'
/** 「指定资源」按钮文案:按类型汇总已选数量。 */
const scopeButtonText = computed(() => {
  if (isContainerMetric.value) return `选择（容器 ${form.target_container_ids.length} 个）`
  return `选择（设备与虚拟机 ${hostSelectedCount.value} 台）`
})
const targetSelectorVisible = ref(false)

function categoryFor(metric: string) {
  return numericMetrics.includes(metric)
    ? 'performance'
    : ['host_status', 'host_online'].includes(metric)
      ? 'host'
      : ['container_status', 'container_running', 'container_restarting'].includes(metric)
        ? 'container'
        : 'business'
}
function onCategoryChange(value: string) {
  const defaults: Record<string, string> = {
    performance: 'cpu_pct',
    host: 'host_status',
    container: 'container_status',
    business: 'business_status',
  }
  form.metric = defaults[value] || 'cpu_pct'
  form.severity = value === 'performance' ? 'warning' : 'critical'
  form.threshold = value === 'performance' ? 90 : 0
  form.operator = 'gt'
  form.target_scope = 'all'
  form.target_device_ids = []
  form.target_container_ids = []
  form.target_business_ids = []
  onMetricChange()
}
function selectCategory(value: string) {
  form.category = value
  onCategoryChange(value)
}
function onTargetScopeChange(value: string) {
  if (value === 'selected') {
    // 与角色管理一致:切到「指定资源」直接弹出选择器(业务规则用内联下拉,无需弹窗)
    if (usesDeviceSelector.value) targetSelectorVisible.value = true
    return
  }
  // 切回「全部」即清空目标;服务端对空目标列表按全部对象评估
  form.target_device_ids = []
  form.target_container_ids = []
  form.target_business_ids = []
}
function onDeviceSelectorConfirm(ids: number[]) {
  form.target_device_ids = ids
}
// 选择器可选判断:仅性能指标规则(cpu/mem/disk)拦"WinRM 确认不通"的 Windows
// 设备——这些指标靠指标采集(Windows=WinRM)喂数据,建了对它只会产出"永远无
// 数据"的死规则。host_status/host_online 走 monitor 的 TCP 探测,与 WinRM
// 无关,不拦。虚拟机:cpu/mem 指标由后端从 PVE API 快照(30s 一轮)评估,可选;
// 磁盘使用率由 QGA get-fsinfo(优先)/运维接入 SSH/WinRM(兑底)采集(后端
// guest_fs_metrics 低频循环),所以要求**配置了运维接入**(has_credential)
// 才可选——没接入的虚机磁盘值永远采不到,建了也是死规则。
// 运行时状态取自 DeviceSelector 暴露的 deviceCache(metrics_failed 字段);
// 虚拟机的 has_credential 由候选接口(automation/devices)随行返回,经
// DeviceSelector 的 selectableFn 第三参传入。
const deviceSelectorRef = ref<InstanceType<typeof DeviceSelector> | null>(null)
const selectorDeviceCache = computed(
  () => deviceSelectorRef.value?.deviceCache ?? new Map<number, { metrics_failed?: boolean }>(),
)
// 虚拟机候选元数据(has_credential),切换指标时清理已选但当前不可选的虚拟机目标
const selectorGuestCache = computed(
  () => deviceSelectorRef.value?.guestCache ?? new Map<number, { has_credential?: boolean }>(),
)
const GUEST_METRIC_SUPPORTED = ['cpu_pct', 'mem_pct']
function targetSelectable(id: number, isGuest: boolean, guest?: { has_credential?: boolean }): boolean {
  const isGuestTarget = isGuest || id < 0
  if (!numericMetrics.includes(form.metric)) return true
  if (isGuestTarget) {
    // 磁盘使用率:虚机数据源是 QGA/运维接入,配置了运维接入才可选
    if (form.metric === 'disk_max_pct') return !!guest?.has_credential
    return GUEST_METRIC_SUPPORTED.includes(form.metric)
  }
  return !selectorDeviceCache.value.get(id)?.metrics_failed
}
function guestSelectableForMetric(id: number, metric: string): boolean {
  if (metric !== 'disk_max_pct') return GUEST_METRIC_SUPPORTED.includes(metric) || !numericMetrics.includes(metric)
  return !!selectorGuestCache.value.get(id)?.has_credential
}
function pruneGuestTargetsForMetric(metric: string) {
  // 切换指标后,已选虚拟机里当前指标不可选的要立即移除,否则会静默保存成死目标
  form.target_device_ids = form.target_device_ids.filter(
    (id: number) => id >= 0 || guestSelectableForMetric(id, metric),
  )
}
// ---- 容器指定:按所属设备分组直接勾选容器,容器规则不再需要先选设备 ----
const containerSearch = ref('')
const containerGroups = computed(() => {
  // 按宿主 target_id 分组:设备为正 id,虚拟机为合成负数 id,两类宿主并列展示。
  const map = new Map<number, { target_id: number; host_kind: string; device_name: string; items: any[] }>()
  for (const item of containers.value) {
    const targetId = Number(item.target_id ?? item.device_id)
    if (!map.has(targetId))
      map.set(targetId, {
        target_id: targetId,
        host_kind: item.host_kind || 'device',
        device_name: item.device_name,
        items: [],
      })
    map.get(targetId)!.items.push(item)
  }
  return [...map.values()]
})
const filteredContainerGroups = computed(() => {
  const keyword = containerSearch.value.trim().toLowerCase()
  if (!keyword) return containerGroups.value
  return containerGroups.value
    .map((group) => ({
      ...group,
      items: group.items.filter(
        (item) =>
          String(item.name || '')
            .toLowerCase()
            .includes(keyword) || group.device_name.toLowerCase().includes(keyword),
      ),
    }))
    .filter((group) => group.items.length > 0)
})
const containerTotalCount = computed(() => containers.value.length)
const isContainerChecked = (item: any) => form.target_container_ids.includes(String(item.container_id))
function toggleContainer(item: any) {
  const id = String(item.container_id)
  const index = form.target_container_ids.indexOf(id)
  if (index >= 0) form.target_container_ids.splice(index, 1)
  else form.target_container_ids.push(id)
}
// 分组(设备)级全选:勾选组名即选中该设备当前过滤出的全部容器
const groupCheckedCount = (group: { items: any[] }) => group.items.filter((item) => isContainerChecked(item)).length
const isGroupAllChecked = (group: { items: any[] }) =>
  group.items.length > 0 && groupCheckedCount(group) === group.items.length
const isGroupIndeterminate = (group: { items: any[] }) => {
  const count = groupCheckedCount(group)
  return count > 0 && count < group.items.length
}
function toggleContainerGroup(group: { items: any[] }, checked: boolean) {
  for (const item of group.items) {
    const id = String(item.container_id)
    const index = form.target_container_ids.indexOf(id)
    if (checked && index < 0) form.target_container_ids.push(id)
    else if (!checked && index >= 0) form.target_container_ids.splice(index, 1)
  }
}
// 工具栏全局全选(作用于搜索过滤后的结果)
const containerAllChecked = computed({
  get: () => {
    const visible = filteredContainerGroups.value.flatMap((group) => group.items)
    return visible.length > 0 && visible.every((item) => isContainerChecked(item))
  },
  set: () => {},
})
const containerAllIndeterminate = computed(() => {
  const visible = filteredContainerGroups.value.flatMap((group) => group.items)
  const count = visible.filter((item) => isContainerChecked(item)).length
  return count > 0 && count < visible.length
})
function onContainerAllChange(checked: boolean) {
  for (const group of filteredContainerGroups.value) toggleContainerGroup(group, checked)
}
function containerStateType(item: any) {
  const state = String(item.state || '').toLowerCase()
  if (state === 'running') return 'ok'
  if (state === 'restarting' || state === 'paused' || state === 'created') return 'warn'
  return 'bad'
}
function onMetricChange() {
  if (!isNumericMetric.value) {
    form.threshold = 0
    form.operator = 'gt'
  }
  // 切换指标后立即清理不可选的虚拟机目标(如切到磁盘时未配置运维接入的虚机)
  pruneGuestTargetsForMetric(form.metric)
}
async function loadTargets() {
  const result = await Promise.all([alertAPI.containers(), alertAPI.businesses()])
  containers.value = result[0].data
  businesses.value = result[1].data
  // 设备 / 虚拟机候选由 DeviceSelector 自行加载(机房与虚拟化分组卡片)
}

async function open(rule?: AlertRule) {
  if (rule) {
    editing.value = rule
    const targetDeviceIds = rule.target_device_ids ? [...rule.target_device_ids] : []
    const targetContainerIds = rule.target_container_ids ? [...rule.target_container_ids] : []
    const targetBusinessIds = rule.target_business_ids ? [...rule.target_business_ids] : []
    Object.assign(form, {
      ...rule,
      category: categoryFor(rule.metric),
      severity: rule.severity,
      // 目标列表为空 = 服务端语义上的「全部资源」
      target_scope:
        targetDeviceIds.length + targetContainerIds.length + targetBusinessIds.length > 0 ? 'selected' : 'all',
      target_device_ids: targetDeviceIds,
      target_container_ids: targetContainerIds,
      target_business_ids: targetBusinessIds,
    })
  } else {
    editing.value = null
    Object.assign(form, {
      name: '',
      category: 'performance',
      metric: 'cpu_pct',
      operator: 'gt',
      threshold: 90,
      severity: 'warning',
      target_scope: 'all',
      target_device_ids: [],
      target_container_ids: [],
      target_business_ids: [],
      sustain_seconds: 60,
      cooldown_seconds: 300,
      enabled: true,
    })
  }
  targetSelectorVisible.value = false
  formVisible.value = true
  await loadTargets()
}
/** 弹窗开着时刷新候选(原 loadAll 的 if (formVisible) await loadTargets() 分支) */
async function refreshTargetsIfOpen() {
  if (formVisible.value) await loadTargets()
}
async function submitRule() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  try {
    const payload = {
      ...form,
      // 状态类规则(主机/容器/业务)固定严重(2026-09-18 用户定调):编辑历史
      // warning/info 规则时也一并收敛,界面不再提供其它选项
      severity: form.category === 'performance' ? form.severity : 'critical',
      threshold: isNumericMetric.value ? form.threshold : 0,
      operator: isNumericMetric.value ? form.operator : 'gt',
      target_device_ids:
        form.target_scope === 'selected' && form.target_device_ids.length ? form.target_device_ids : null,
      target_container_ids:
        form.target_scope === 'selected' && form.target_container_ids.length ? form.target_container_ids : null,
      target_business_ids:
        form.target_scope === 'selected' && form.target_business_ids.length ? form.target_business_ids : null,
    }
    if (editing.value) await alertAPI.updateRule(editing.value.id, payload)
    else await alertAPI.createRule(payload)
    ElMessage.success('告警规则已保存')
    formVisible.value = false
    emit('saved')
  } catch (e) {
    ElMessage.error(errText(e, '保存告警规则失败'))
  } finally {
    saving.value = false
  }
}

defineExpose({ open, refreshTargetsIfOpen })
</script>

<style scoped>
:deep(.rule-dialog) {
  margin-top: 5vh !important;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 24px 70px color-mix(in srgb, #000 28%, transparent);
}
:deep(.rule-dialog .el-dialog__header) {
  padding: 20px 26px;
  border-bottom: 1px solid var(--dcn-border);
}
:deep(.rule-dialog .el-dialog__title) {
  font-size: 18px;
  font-weight: 700;
}
:deep(.rule-dialog .el-dialog__body) {
  max-height: calc(90vh - 150px);
  overflow: auto;
  padding: 20px 24px;
  background: var(--dcn-bg-page);
}
:deep(.rule-dialog .el-dialog__footer) {
  padding: 15px 24px;
  border-top: 1px solid var(--dcn-border);
  background: var(--dcn-bg-card);
}
:deep(.rule-dialog .el-dialog__footer .el-button) {
  min-width: 92px;
  min-height: 40px;
}
:deep(.rule-dialog .el-form-item) {
  margin-bottom: 16px;
}
:deep(.rule-dialog .el-form-item__label) {
  padding-bottom: 7px;
  color: var(--dcn-text-primary);
  font-weight: 600;
}
:deep(.rule-dialog .el-select__wrapper),
:deep(.rule-dialog .el-input__wrapper) {
  min-height: 42px;
}
.rule-section {
  margin-bottom: 16px;
  padding: 18px;
  border: 1px solid var(--dcn-border);
  border-radius: 12px;
  background: var(--dcn-bg-card);
}
.rule-section:last-of-type {
  margin-bottom: 14px;
}
.rule-section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
}
.rule-section-title > span {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--dcn-primary) 12%, transparent);
  color: var(--dcn-primary);
  font: 600 11px var(--dcn-font-mono);
}
.rule-section-title > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rule-section-title strong {
  font-size: 14px;
  color: var(--dcn-text-primary);
}
.rule-section-title small {
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
.type-card-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.type-card {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
  min-height: 72px;
  padding: 12px;
  border: 1px solid var(--dcn-border);
  border-radius: 10px;
  background: var(--dcn-bg-section);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease,
    box-shadow 0.16s ease;
}
.type-card:hover {
  border-color: color-mix(in srgb, var(--dcn-primary) 45%, var(--dcn-border));
  background: color-mix(in srgb, var(--dcn-primary) 4%, var(--dcn-bg-section));
}
.type-card:focus-visible {
  outline: 2px solid var(--dcn-primary);
  outline-offset: 2px;
}
.type-card.active {
  border-color: var(--dcn-primary);
  background: color-mix(in srgb, var(--dcn-primary) 8%, var(--dcn-bg-card));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--dcn-primary) 28%, transparent);
}
.type-card-icon {
  display: grid;
  place-items: center;
  flex: 0 0 38px;
  height: 38px;
  border-radius: 10px;
  background: var(--dcn-bg-card);
  color: var(--dcn-text-secondary);
  font-size: 19px;
}
.type-card.active .type-card-icon {
  background: color-mix(in srgb, var(--dcn-primary) 14%, transparent);
  color: var(--dcn-primary);
}
.type-card-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.type-card-copy strong {
  font-size: 13px;
  color: var(--dcn-text-primary);
}
.type-card-copy small {
  font-size: 11px;
  color: var(--dcn-text-secondary);
  white-space: normal;
}
.type-card-check {
  position: absolute;
  right: 10px;
  top: 9px;
  display: none;
  color: var(--dcn-primary);
  font-size: 12px;
}
.type-card.active .type-card-check {
  display: block;
}
.policy-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.policy-grid .el-form-item {
  position: relative;
}
.input-suffix {
  position: absolute;
  right: 12px;
  bottom: 12px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
  pointer-events: none;
}
.policy-hint {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--dcn-bg-section);
  color: var(--dcn-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.fixed-metric {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid var(--dcn-border);
  border-radius: 8px;
  background: var(--dcn-bg-section);
  color: var(--dcn-text-primary);
}
.fixed-metric > span:nth-child(2) {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.fixed-metric strong {
  font-size: 13px;
}
.fixed-metric small {
  font-size: 10px;
  color: var(--dcn-text-secondary);
}
.fixed-metric-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--dcn-primary);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--dcn-primary) 12%, transparent);
}
.target-form-item {
  margin-bottom: 0 !important;
}
.target-form-item :deep(.el-form-item__content) {
  display: block;
}
.target-scope {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  flex-wrap: wrap;
}
.target-scope :deep(.el-radio-group) {
  flex: 0 0 auto;
}
.target-business-select {
  flex: 1;
  min-width: 240px;
}
.scope-tip {
  margin-top: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--dcn-bg-section);
  color: var(--dcn-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.container-picker {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.container-picker-toolbar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.container-picker-search {
  flex: 1;
  min-width: 220px;
}
.container-toolbar-side {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}
.container-groups {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 320px;
  overflow: auto;
  /* 滚动条贴边时给卡片留呼吸位,避免视觉上贴在一起 */
  padding-right: 6px;
  padding-bottom: 2px;
}
.container-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2px 6px;
}
.container-group-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--dcn-text-primary);
  font-size: 12px;
  font-weight: 600;
}
.container-group-name .el-icon {
  color: var(--dcn-primary-light);
}
.host-kind-tag {
  margin-left: 6px;
}
.container-group-count {
  color: var(--dcn-text-secondary);
  font-size: 11px;
}
.container-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}
.container-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-card);
  cursor: pointer;
  transition:
    border-color 0.15s,
    background-color 0.15s;
}
.container-card:hover {
  border-color: var(--dcn-primary);
}
.container-card.checked {
  border-color: var(--dcn-primary);
  background: var(--dcn-bg-active);
}
.container-state-dot {
  flex: 0 0 7px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.container-state-dot.ok {
  background: var(--dcn-success);
}
.container-state-dot.warn {
  background: var(--dcn-warning);
}
.container-state-dot.bad {
  background: var(--dcn-danger);
}
.container-card-info {
  flex: 1;
  min-width: 0;
}
.container-card-name {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.container-card-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 1px;
  color: var(--dcn-text-secondary);
  font-size: 11px;
}
.rule-enable {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border: 1px solid var(--dcn-border);
  border-radius: 11px;
  background: var(--dcn-bg-card);
}
.rule-enable > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.rule-enable strong {
  font-size: 13px;
}
.rule-enable span {
  font-size: 11px;
  color: var(--dcn-text-secondary);
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.field-help {
  margin-left: 8px;
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.object-help {
  display: block;
  margin: 6px 0 0;
}
@media (max-width: 760px) {
  :deep(.rule-dialog) {
    width: calc(100vw - 20px) !important;
    margin-top: 2vh !important;
  }
  .type-card-grid,
  .form-grid,
  .policy-grid {
    grid-template-columns: 1fr;
  }
  :deep(.rule-dialog .el-dialog__body) {
    max-height: calc(96vh - 140px);
    padding: 14px;
  }
  .rule-section {
    padding: 14px;
  }
}
.policy-grid.state-policy-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.policy-grid.state-policy-grid .el-form-item {
  min-width: 0;
}
@media (max-width: 760px) {
  .policy-grid.state-policy-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 600px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
