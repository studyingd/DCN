import type { Component } from 'vue'
import { Bell, ChatDotRound, ChatDotSquare, ChatLineRound, Link, Message, Share } from '@element-plus/icons-vue'

/** 飞书自建应用固定使用国内开放平台域名，界面上不再暴露该配置项 */
export const FEISHU_BASE = 'https://open.feishu.cn'
export const WEBHOOK_PLACEHOLDER = 'https://example.com/webhook'
/** 与后端 feishu._MAX_RECEIVERS 一致 */
export const MAX_RECEIVERS = 50

export interface ProviderOption {
  value: string
  label: string
  hint: string
  icon: Component
}
export const providers: ProviderOption[] = [
  { value: 'feishu_app', label: '飞书自建应用', hint: '直接私聊指定的人，可从通讯录选人', icon: Message },
  { value: 'feishu_bot', label: '飞书群机器人', hint: '发到机器人所在的群，支持加签', icon: ChatDotSquare },
  { value: 'feishu', label: '飞书工作流', hint: '多维表格 / 审批流程的 Webhook', icon: Share },
  { value: 'wecom', label: '企业微信', hint: '群机器人 Webhook，POST JSON', icon: ChatDotRound },
  { value: 'dingtalk', label: '钉钉', hint: '群机器人 Webhook，支持加签', icon: ChatLineRound },
  { value: 'generic', label: '通用 HTTP', hint: '任意接收端，POST 完整告警 JSON', icon: Link },
]

export const eventOptions = [
  { value: 'alert.created', label: '告警触发', hint: '指标越限或主机 / 容器离线' },
  { value: 'alert.resolved', label: '告警恢复', hint: '指标回落或对象恢复在线' },
  { value: 'alert.remediation', label: '自动处置进度', hint: '虚拟机 / 容器自动拉起的每个阶段' },
  {
    value: 'alert.analysis',
    label: 'AI 归因结论',
    hint: '兜底：仅看门狗超时先发告警卡时，归因完成后补发',
  },
  {
    value: 'automation.notify',
    label: '自动化通知',
    hint: '自动化任务结论，巡检附 PDF',
  },
]
const eventMap = Object.fromEntries(eventOptions.map((item) => [item.value, item.label]))

export function providerIcon(value: string) {
  return providers.find((item) => item.value === value)?.icon || Bell
}
export function providerLabel(value: string) {
  return providers.find((item) => item.value === value)?.label || value
}
export function eventLabel(value: string) {
  return eventMap[value] || value
}

export const ID_TYPE_LABEL: Record<string, string> = {
  email: '邮箱',
  open_id: 'open_id',
  union_id: 'union_id',
  chat_id: '群',
  user_id: 'user_id',
}
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
/** 与后端 feishu.infer_receive_id_type 一致:界面不再让用户挑 ID 类型 */
export function inferIdType(value: string) {
  const text = value.trim()
  if (EMAIL_PATTERN.test(text)) return 'email'
  if (text.startsWith('ou_')) return 'open_id'
  if (text.startsWith('on_')) return 'union_id'
  if (text.startsWith('oc_')) return 'chat_id'
  return 'user_id'
}

export interface ReceiverChip {
  id: string
  name: string
  typeLabel: string
}
export function chipOf(id: string, name?: string): ReceiverChip {
  return { id, name: name || id, typeLabel: ID_TYPE_LABEL[inferIdType(id)] || id }
}

/** FeishuDirectoryDialog.open() 的上下文:打开时冻结的凭据与接收人快照 */
export interface DirectoryOpenContext {
  webhookId: number | null
  /** 可选与 FeishuDirectoryRequest.base 对齐(WebhookCreate.url 本就可选) */
  base?: string
  appId: string
  appSecret?: string
  /** 编辑已有通道且 Secret 未重填时,后端可回落库里加密存的那份 */
  secretSet: boolean
  /** 打开时接收人列表快照,用于把已有接收人回显成勾选状态 */
  receivers: string[]
  receiverNames: Record<string, string>
}
