import { ref, type Ref } from 'vue'
import { ElMessage } from 'element-plus'
import { automationAPI } from '@/api'
import type { AutomationDevice, AutomationJobType } from '@/types/automation'

/**
 * 自动化目标设备列表(机房设备 + PVE 虚机,合成负数 id)。懒加载:
 * 创建向导与计划编辑打开时才拉取,概览页不请求。
 */
export function useAutomationDevices() {
  const devices = ref<AutomationDevice[]>([])
  const loading = ref(false)
  const loaded = ref(false)

  async function load(force = false) {
    if (loaded.value && !force) return
    loading.value = true
    try {
      devices.value = (await automationAPI.devices()).data
      loaded.value = true
    } catch {
      ElMessage.error('加载设备列表失败')
    } finally {
      loading.value = false
    }
  }

  return { devices, loading, load }
}

/**
 * DeviceSelector 的可选判断(按 id 反查 devices,选择器只传 id)。
 * jobType 由调用方以 getter 传入——创建向导传向导当前类型,计划编辑传
 * 被编辑计划的类型(旧实现在编辑计划时误读创建向导的 jobType,导致
 * power 计划的虚机目标被按「需凭据」规则误禁)。
 *
 * PVE 虚拟机:电源操作始终可选(走 PVE API,无需 guest 凭据);其余任务类型
 * (巡检/批量脚本/Agent)都是连进 guest 内部执行(SSH/WinRM),必须先配置好
 * 运维接入凭据和 IP——与后端 create_job 对 pve_guest 非 power 任务的校验一致。
 * 普通设备:与 PVE 虚拟机口径统一——没绑凭据/没 IP 的一律不可选。
 * Windows 设备额外看通道连通信号:指标采集对 Windows 走的就是 WinRM,
 * 「最近采集明确失败」= WinRM 确认不通 → 不可选(修好或下一轮采集成功
 * 自动恢复);「还没采集过/成功」不拦,避免误伤刚恢复的机器。
 */
export function deviceSelectable(devices: Ref<AutomationDevice[]>, jobType: () => AutomationJobType) {
  return (id: number, isGuest: boolean): boolean => {
    const device = devices.value.find((d) => d.id === id)
    if (!device) return false
    if (device.target_type === 'pve_guest' || isGuest) {
      if (jobType() === 'power') return true
      return !!device.has_credential && !!device.ip_address
    }
    return !!device.has_credential && !!device.ip_address && !device.metrics_failed
  }
}
