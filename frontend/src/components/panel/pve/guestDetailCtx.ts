import type { Ref } from 'vue'
import type { PveGuest } from '@/types/pve'
import type { usePveGuestDetail } from '@/composables/usePveGuestDetail'

type DetailCtl = ReturnType<typeof usePveGuestDetail>

/**
 * 详情抽屉 + 运维接入弹窗的上下文（PvePanel → GuestDetailDrawer 单 prop 契约）。
 *
 * 状态全部留在 PvePanel（usePveGuestDetail 工厂实例与电源/控制台/删除动作），
 * 以单 prop 对象传入子组件；子组件在 setup 里解构，ref 经解构保持响应式，
 * 模板与拆分前的面板 1:1（v-model 直接写穿回面板状态）。
 */
export interface GuestDetailCtx extends Pick<
  DetailCtl,
  | 'detailVisible'
  | 'detail'
  | 'detailLoading'
  | 'onDetailClosed'
  | 'guestBinding'
  | 'bindingLoading'
  | 'bindingTestLoading'
  | 'winrmScriptLoading'
  | 'openBindingForm'
  | 'testGuestAccess'
  | 'downloadGuestWinrmScript'
  | 'bindingVisible'
  | 'bindingForm'
  | 'bindingSaving'
  | 'bindingOsDetection'
  | 'saveGuestBinding'
  | 'snapshots'
  | 'historyRange'
  | 'historyPoints'
  | 'historyLoading'
  | 'historyError'
  | 'loadHistory'
  | 'snapName'
  | 'snapBusy'
  | 'createSnap'
  | 'rollbackSnap'
  | 'deleteSnap'
  | 'openConfigForm'
> {
  /** 电源操作（面板实现：确认弹窗 + 延迟重载 + fromDetail 时刷新详情） */
  power: (g: PveGuest, action: string, fromDetail?: boolean) => Promise<void>
  /** 电源操作忙碌标记（`key(g, action)` 寻址） */
  powerBusy: Ref<string | null>
  /** 打开 noVNC 控制台 */
  openConsole: (g: PveGuest) => void
  /** 打开删除预览弹窗 */
  openDelete: (g: PveGuest) => void
}
