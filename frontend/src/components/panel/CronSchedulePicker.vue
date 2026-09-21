<script setup lang="ts">
/**
 * 周期计划选择器（cron 可视化构建）。
 *
 * 存在的理由：周期执行原先只有一个裸文本框要求用户手写 cron（`0 2 * * *`），
 * 健康巡检这类高频操作被迫去记 5 段语法，写错还要等后端报错才知道。
 * 这里改成「选频率 + 选时间 + 勾选星期」，实时给出中文预览。
 *
 * 不提供手写 cron 的入口，因此构建器必须覆盖运维真正需要的周期——尤其是
 * 「工作日」「周末」这类多天组合（周字段生成 `1-5` / `0,6`）。
 *
 * 遇到构建器表达不了的既有表达式（如一天两次的 `0 2,14 * * *`），进入**只读锁定**
 * 状态：原样展示、原样提交，绝不猜一个近似周期覆盖它。用户必须显式点
 * 「改用可视化编辑」才会重置——静默改写会让人以为改的是原计划，
 * 实际提交的是另一个周期。
 *
 * v-model 是 cron 表达式字符串；构建/解析/中文化全部走 utils/cron.ts，
 * 与 ScriptPanel 的计划任务共用同一套逻辑。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { Timer } from '@element-plus/icons-vue'
import { DEFAULT_CRON_STATE, WEEKDAY_LABELS, buildCron, humanizeCron, parseCron, type CronState } from '@/utils/cron'

const props = defineProps<{
  modelValue?: string | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [string]
}>()

const builder = reactive<CronState>(freshState())
/** 构建器表达不了的既有表达式；非空时为只读锁定状态 */
const lockedExpr = ref('')

function freshState(): CronState {
  return { ...DEFAULT_CRON_STATE, weekdays: [...DEFAULT_CRON_STATE.weekdays] }
}

// el-time-picker 要 Date，这里双向桥接到 builder 的 hour / timeMinute
const timeOfDay = computed<Date>({
  get: () => new Date(2024, 0, 1, builder.hour, builder.timeMinute),
  set: (d) => {
    if (!d) return
    builder.hour = d.getHours()
    builder.timeMinute = d.getMinutes()
  },
})

const intervalMax = computed(() => (builder.mode === 'minutes' ? 59 : 23))

// 勾选顺序按中文习惯：周一…周六、周日最后。cron 字段本身仍按 0-6 升序写。
const PICKER_ORDER = [1, 2, 3, 4, 5, 6, 0] as const

const WEEKDAY_PRESETS: { label: string; days: number[] }[] = [
  { label: '每天', days: [0, 1, 2, 3, 4, 5, 6] },
  { label: '工作日', days: [1, 2, 3, 4, 5] },
  { label: '周末', days: [0, 6] },
]

const locked = computed(() => !!lockedExpr.value)

function currentExpr(): string {
  return lockedExpr.value || buildCron(builder)
}

const preview = computed(() => humanizeCron(currentExpr()))

function emitChange() {
  const next = currentExpr()
  if (next !== (props.modelValue ?? '').trim()) emit('update:modelValue', next)
}

function applyPreset(days: number[]) {
  builder.weekdays = [...days]
}

function presetActive(days: number[]): boolean {
  if (builder.mode !== 'weekly') return false
  const a = [...builder.weekdays].sort((x, y) => x - y)
  const b = [...days].sort((x, y) => x - y)
  return a.length === b.length && a.every((v, i) => v === b[i])
}

/** 锁定状态下显式解锁：重置成默认周期并回传，用户是知情同意的 */
function unlockToBuilder() {
  lockedExpr.value = ''
  Object.assign(builder, freshState())
  emitChange()
}

// 构建器状态 → v-model（锁定时 currentExpr 返回原值，不会误改）
watch(builder, emitChange)

// v-model → 内部状态。onMounted 与 watch 共用，避免两处逻辑漂移。
function syncFromModel() {
  const incoming = (props.modelValue ?? '').trim()
  if (incoming === currentExpr()) return // 自己发出去的回声

  if (!incoming) {
    // 父级清空了值（典型场景：ScriptPanel 的对话框没有 destroy-on-close，
    // 「编辑任务 A」关闭后再点「新建」，组件并未重新挂载）。
    // 此时必须回到默认值并回传，否则预览还显示着上一个任务的周期、
    // 表单值却是空的，提交时会被必填校验拦住——用户明明看着有选择。
    lockedExpr.value = ''
    Object.assign(builder, freshState())
    emit('update:modelValue', buildCron(builder))
    return
  }

  const parsed = parseCron(incoming)
  if (parsed) {
    lockedExpr.value = ''
    Object.assign(builder, parsed)
  } else {
    // 表达不了就锁住原值只读展示，绝不拿猜测值覆盖用户已有的计划
    lockedExpr.value = incoming
  }
}

watch(() => props.modelValue, syncFromModel)

// 新建场景下 modelValue 一进来就是空，watch 不会触发，需要主动同步一次：
// 预填一个「每天 08:00」，让必填项一进来就有可见的默认值，而不是留一个
// 空文本框等用户去回忆 cron 语法。
onMounted(syncFromModel)
</script>

<template>
  <div class="cron-picker">
    <!-- 只读锁定：既有周期无法用可视化方式表达 -->
    <div v-if="locked" class="cron-locked">
      <div class="cron-locked-main">
        <span class="cron-locked-label">当前周期</span>
        <span class="cron-locked-human">复杂自定义周期（按原表达式保存与执行）</span>
      </div>
      <div class="cron-locked-hint">
        这个周期比可视化选项更复杂（例如一天多次、指定月份），无法在此编辑。 保持现状则原样保存，不会被改动。
      </div>
      <el-button size="small" :disabled="disabled" @click="unlockToBuilder">
        改用可视化编辑（将重置为每天 08:00）
      </el-button>
    </div>

    <template v-else>
      <div class="schedule-builder">
        <div class="schedule-row">
          <span class="schedule-label">频率</span>
          <el-select v-model="builder.mode" :disabled="disabled" style="width: 160px">
            <el-option label="每隔几分钟" value="minutes" />
            <el-option label="每隔几小时" value="hours" />
            <el-option label="每天" value="daily" />
            <el-option label="每周" value="weekly" />
            <el-option label="每月" value="monthly" />
          </el-select>
        </div>

        <div v-if="builder.mode === 'minutes'" class="schedule-row">
          <span class="schedule-label">间隔</span>
          <el-input-number
            v-model="builder.interval"
            :controls="false"
            :min="1"
            :max="59"
            :disabled="disabled"
            style="width: 120px"
          />
          <span class="schedule-unit">分钟</span>
        </div>

        <div v-if="builder.mode === 'hours'" class="schedule-row">
          <span class="schedule-label">间隔</span>
          <el-input-number
            v-model="builder.interval"
            :controls="false"
            :min="1"
            :max="intervalMax"
            :disabled="disabled"
            style="width: 120px"
          />
          <span class="schedule-unit">小时</span>
          <span class="schedule-label schedule-label-inline">在第</span>
          <el-input-number
            v-model="builder.minute"
            :controls="false"
            :min="0"
            :max="59"
            :disabled="disabled"
            style="width: 100px"
          />
          <span class="schedule-unit">分执行</span>
        </div>

        <div v-if="builder.mode === 'daily'" class="schedule-row">
          <span class="schedule-label">每天</span>
          <el-time-picker
            v-model="timeOfDay"
            format="HH:mm"
            placeholder="选择时间"
            :disabled="disabled"
            style="width: 140px"
          />
          <span class="schedule-unit">执行</span>
        </div>

        <!-- 每周：星期可多选（工作日/周末/任意组合） -->
        <template v-if="builder.mode === 'weekly'">
          <div class="schedule-row schedule-row-weekdays">
            <span class="schedule-label">重复</span>
            <el-checkbox-group v-model="builder.weekdays" :disabled="disabled">
              <el-checkbox-button v-for="d in PICKER_ORDER" :key="d" :value="d">
                {{ WEEKDAY_LABELS[d] }}
              </el-checkbox-button>
            </el-checkbox-group>
          </div>
          <div class="schedule-row">
            <span class="schedule-label">快捷</span>
            <el-button
              v-for="preset in WEEKDAY_PRESETS"
              :key="preset.label"
              size="small"
              :type="presetActive(preset.days) ? 'primary' : 'default'"
              :disabled="disabled"
              @click="applyPreset(preset.days)"
            >
              {{ preset.label }}
            </el-button>
            <span v-if="!builder.weekdays.length" class="schedule-warning">请至少选择一天</span>
          </div>
          <div class="schedule-row">
            <span class="schedule-label">时间</span>
            <el-time-picker
              v-model="timeOfDay"
              format="HH:mm"
              placeholder="选择时间"
              :disabled="disabled"
              style="width: 140px"
            />
            <span class="schedule-unit">执行</span>
          </div>
        </template>

        <div v-if="builder.mode === 'monthly'" class="schedule-row">
          <span class="schedule-label">每月</span>
          <el-input-number
            v-model="builder.day"
            :controls="false"
            :min="1"
            :max="31"
            :disabled="disabled"
            style="width: 120px"
          />
          <span class="schedule-unit">日</span>
          <el-time-picker
            v-model="timeOfDay"
            format="HH:mm"
            placeholder="选择时间"
            :disabled="disabled"
            style="width: 140px"
          />
          <span class="schedule-unit">执行</span>
        </div>
      </div>

      <!-- 实时预览：只给中文结论，不展示 cron 原文（运维不需要看表达式） -->
      <div class="schedule-preview">
        <el-icon><Timer /></el-icon>
        <span>{{ preview }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.cron-picker {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}

.schedule-builder {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
}

.schedule-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  flex-wrap: wrap;
}

.schedule-row-weekdays {
  align-items: flex-start;
}

.schedule-label {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
  flex-shrink: 0;
}

.schedule-label-inline {
  margin-left: var(--dcn-space-3);
}

.schedule-unit {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
}

.schedule-warning {
  font-size: var(--dcn-text-sm);
  color: var(--el-color-warning);
}

.schedule-preview {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: var(--dcn-space-2) var(--dcn-space-3);
  background: var(--dcn-success-light);
  border-radius: var(--dcn-radius-sm);
  font-size: var(--dcn-text-base);
  color: var(--dcn-success);
}

.cron-locked {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-3);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-page);
}

.cron-locked-main {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}

.cron-locked-label {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-secondary);
}

.cron-locked-human {
  font-size: var(--dcn-text-base);
  color: var(--dcn-text-regular);
}

.cron-locked-hint {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  line-height: 1.5;
}

.cron-locked > .el-button {
  align-self: flex-start;
}
</style>
