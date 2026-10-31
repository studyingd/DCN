<template>
  <el-drawer
    v-model="detailVisible"
    :title="detail?.name || '虚拟机详情'"
    size="min(680px, 94vw)"
    direction="rtl"
    @closed="onDetailClosed"
  >
    <div v-if="detail" v-loading="detailLoading" class="g-body">
      <div class="g-head">
        <el-tag :type="detail.status === 'running' ? 'success' : 'info'" size="large" effect="plain">
          {{ detail.status === 'running' ? '运行中' : '已停止' }}
        </el-tag>
        <span class="g-sub">QEMU 虚拟机 · VMID {{ detail.vmid }} · 节点 {{ detail.node }}</span>
      </div>

      <div class="g-section-title">运维接入</div>
      <div class="g-access-card">
        <div class="g-access-status-row">
          <el-tag :type="guestBinding?.configured ? 'success' : 'info'" effect="plain" size="small">
            {{ guestBinding?.configured ? '运维已配置' : '未配置' }}
          </el-tag>
          <el-tag v-if="guestBinding?.qga_available" type="success" effect="plain" size="small"
            >QEMU Guest Agent 已连接</el-tag
          >
          <el-tag v-else-if="guestBinding?.qga_enabled" type="warning" effect="plain" size="small"
            >QEMU Guest Agent 不可用，已回退</el-tag
          >
        </div>
        <div class="g-access-grid">
          <div class="g-access-detail">
            <span>操作系统</span>
            <strong>{{ formatGuestOs(guestBinding) }}</strong>
          </div>
          <div class="g-access-detail">
            <span>虚拟机 IP</span>
            <strong>{{ guestBinding?.ip_address || '未获取' }}</strong>
          </div>
          <div class="g-access-detail">
            <span>接入方式</span>
            <strong>{{
              guestBinding?.os_system === 'windows' ? 'WinRM' : guestBinding?.os_system === 'linux' ? 'SSH' : '待识别'
            }}</strong>
          </div>
          <div class="g-access-detail">
            <span>数据来源</span>
            <strong>{{ guestBinding?.qga_available ? 'QGA 实时信息' : 'PVE / 手动配置' }}</strong>
          </div>
        </div>
        <div v-if="!guestBinding?.configured" class="g-access-summary g-access-summary-block">
          配置虚拟机凭据后，可在自动化运维中执行 Agent 诊断。
        </div>
        <div class="g-access-actions">
          <el-button
            v-if="canConfigureAccess"
            size="small"
            plain
            type="primary"
            :loading="bindingLoading"
            @click="openBindingForm"
            >配置运维接入</el-button
          >
          <el-button
            v-if="canConfigureAccess && guestBinding?.configured"
            size="small"
            plain
            :loading="bindingTestLoading"
            @click="testGuestAccess"
            >测试连接</el-button
          >
        </div>
        <div v-if="guestBinding?.last_test_status === 'failed'" class="g-access-error">
          上次测试失败：{{ guestBinding.last_test_error || '未知错误' }}
        </div>
        <div
          v-if="
            guestBinding?.os_system === 'windows' &&
            guestBinding?.ip_address &&
            !guestBinding?.winrm_available &&
            guestBinding?.last_test_status !== 'success'
          "
          class="g-winrm-tools"
        >
          <el-button size="small" type="warning" plain :loading="winrmScriptLoading" @click="downloadGuestWinrmScript">
            下载 WinRM 启用脚本
          </el-button>
          <span class="binding-hint">脚本按当前 DCN 出口 IP 动态生成，仅允许本机访问 WinRM。</span>
        </div>
      </div>

      <div class="g-section-title">电源</div>
      <el-text v-if="isTemplate(detail)" type="info" size="small" class="g-template-note"
        >这是模板(克隆镜像),不能执行电源操作</el-text
      >
      <div v-else class="g-actions">
        <el-button
          v-if="canOperate && detail.status !== 'running'"
          type="success"
          :loading="powerBusy === key(detail, 'start')"
          @click="power(detail, 'start', true)"
          >启动</el-button
        >
        <el-button
          v-if="canOperate && detail.status === 'running'"
          type="danger"
          :loading="powerBusy === key(detail, 'stop')"
          @click="power(detail, 'stop', true)"
          >停止</el-button
        >
        <el-button
          v-if="canOperate && detail.status === 'running'"
          type="warning"
          :loading="powerBusy === key(detail, 'shutdown')"
          @click="power(detail, 'shutdown', true)"
          >关机</el-button
        >
        <el-button
          v-if="canOperate && detail.status === 'running'"
          type="warning"
          :loading="powerBusy === key(detail, 'reboot')"
          @click="power(detail, 'reboot', true)"
          >重启</el-button
        >
        <el-button v-if="canOperate && detail.status === 'stopped'" plain @click="openConfigForm">调整配置</el-button>
        <el-button
          v-if="canOperate && detail.status === 'running'"
          type="danger"
          plain
          :loading="powerBusy === key(detail, 'reset')"
          @click="power(detail, 'reset', true)"
          >重置</el-button
        >
        <el-button
          v-if="canOperateConsole && detail.status === 'running'"
          type="primary"
          plain
          @click="openConsole(detail)"
          >控制台</el-button
        >
      </div>

      <div class="g-section-title">资源占用</div>
      <div class="g-metrics">
        <div class="g-metric-row">
          <span class="g-metric-label">CPU</span>
          <el-progress
            :percentage="cpuPct(detail)"
            :color="metricColor(cpuPct(detail), 'cpu')"
            :stroke-width="8"
            class="g-bar"
          />
        </div>
        <div class="g-metric-row">
          <span class="g-metric-label">内存</span>
          <el-progress
            :percentage="memPct(detail)"
            :color="metricColor(memPct(detail), 'memory')"
            :stroke-width="8"
            class="g-bar"
          />
        </div>
        <div class="g-mem-detail">内存使用 {{ fmtBytes(detail.mem) }} / {{ fmtBytes(detail.maxmem) }}</div>
        <div class="g-metric-row">
          <span class="g-metric-label">磁盘</span>
          <span class="g-disk-val">已分配 {{ fmtBytes(detail.maxdisk) }}</span>
        </div>
        <template v-if="detail.guest_agent?.filesystem_available && detail.guest_agent.filesystem_disks?.length">
          <div class="g-disk-list">
            <div v-for="disk in sortedDisks(detail.guest_agent.filesystem_disks)" :key="disk.mount" class="g-disk-card">
              <div class="g-disk-card-head">
                <span class="g-disk-mount" :title="disk.mount">{{ disk.mount }}</span>
                <strong>{{ diskPct(disk) }}%</strong>
              </div>
              <el-progress
                :percentage="diskPct(disk)"
                :color="metricColor(diskPct(disk), 'disk')"
                :stroke-width="8"
                :show-text="false"
              />
              <div class="g-disk-card-foot">
                <span>已用 {{ fmtDiskBytes(disk.used_bytes) }}</span>
                <span>总量 {{ fmtBytes(disk.total_bytes) }}</span>
              </div>
            </div>
          </div>
          <div class="g-mem-detail">
            文件系统使用量来自 {{ filesystemSourceLabel(detail.guest_agent.filesystem_source) }}；上方“已分配”为 PVE
            虚拟磁盘容量。
          </div>
        </template>
        <div v-else class="g-mem-detail">
          {{ detail.guest_agent?.filesystem_error || '未获取到客户机文件系统数据，当前仅显示 PVE 分配容量。' }}
        </div>
      </div>

      <div class="g-section-title">数据吞吐</div>
      <div class="throughput-grid">
        <div class="throughput-card">
          <span>磁盘读取</span>
          <strong>{{ fmtRate(detail.disk_read_rate, detail.disk_rate_unavailable) }}</strong>
          <small>累计 {{ fmtBytes(detail.diskread) }}</small>
        </div>
        <div class="throughput-card">
          <span>磁盘写入</span>
          <strong>{{ fmtRate(detail.disk_write_rate, detail.disk_rate_unavailable) }}</strong>
          <small>累计 {{ fmtBytes(detail.diskwrite) }}</small>
        </div>
        <div class="throughput-card">
          <span>网络接收</span>
          <strong>{{ fmtRate(detail.net_in_rate, detail.net_rate_unavailable) }}</strong>
          <small>累计 {{ fmtBytes(detail.netin) }}</small>
        </div>
        <div class="throughput-card">
          <span>网络发送</span>
          <strong>{{ fmtRate(detail.net_out_rate, detail.net_rate_unavailable) }}</strong>
          <small>累计 {{ fmtBytes(detail.netout) }}</small>
        </div>
      </div>
      <p class="throughput-note">实时速率基于最近两次刷新计算；累计量和历史趋势来自 PVE 原生指标，无需 Guest Agent。</p>

      <div class="g-section-head">
        <div class="g-section-title">历史趋势</div>
        <el-select
          v-model="historyRange"
          size="small"
          class="history-range"
          aria-label="历史趋势时间范围"
          @change="loadHistory"
        >
          <el-option label="最近 1 小时" value="1h" />
          <el-option label="最近 24 小时" value="24h" />
          <el-option label="最近 7 天" value="7d" />
          <el-option label="最近 30 天" value="30d" />
          <el-option label="最近 1 年" value="1y" />
        </el-select>
      </div>
      <div v-loading="historyLoading" class="history-shell" aria-live="polite">
        <PveHistoryChart v-if="historyPoints.length" :points="historyPoints" />
        <el-empty v-else :description="historyError || '暂无历史数据'" :image-size="44">
          <el-button v-if="historyError" size="small" @click="loadHistory">重新加载</el-button>
        </el-empty>
      </div>

      <div class="g-section-title">快照</div>
      <div class="g-snap-actions">
        <el-input v-model="snapName" placeholder="快照名(如 before_upgrade)" size="small" class="g-snap-input" />
        <el-button v-if="canOperate" size="small" type="primary" plain :loading="snapBusy" @click="createSnap"
          >创建快照</el-button
        >
      </div>
      <div class="g-snaps">
        <div v-for="s in snapshots" :key="s.name" class="g-snap">
          <div class="g-snap-info">
            <span class="g-snap-name">{{ s.name }}</span>
            <span class="g-snap-time">{{ s.snaptime ? fmtDate(s.snaptime) : '' }}</span>
            <div class="g-snap-desc">{{ s.description || '' }}</div>
          </div>
          <div class="g-snap-actions">
            <el-button v-if="canOperate" link type="warning" size="small" @click="rollbackSnap(s)">回滚</el-button>
            <el-button v-if="canOperate" link type="danger" size="small" @click="deleteSnap(s)">删除</el-button>
          </div>
        </div>
        <el-empty v-if="snapshots.length === 0" description="暂无快照" :image-size="40" />
      </div>

      <div class="g-section-title">危险操作</div>
      <div class="g-actions">
        <el-button v-if="canOperate" type="danger" plain @click="openDelete(detail)">删除虚拟机</el-button>
      </div>
    </div>
  </el-drawer>

  <el-dialog v-model="bindingVisible" title="配置虚拟机运维接入" width="620px" append-to-body class="binding-dialog">
    <div class="binding-intro">
      <div class="binding-intro-mark">OPS</div>
      <div>
        <div class="binding-intro-title">为当前虚拟机配置安全的远程运维通道</div>
        <div class="binding-intro-sub">保存后可在自动化运维中执行诊断、巡检和只读命令。</div>
      </div>
    </div>
    <el-form label-position="top" class="binding-form">
      <section class="binding-section">
        <div class="binding-section-head">
          <span class="binding-section-index">01</span>
          <div>
            <h4>系统与网络</h4>
            <p>确认客户机类型和用于连接的地址。</p>
          </div>
        </div>
        <div v-if="guestBinding?.qga_available" class="binding-detected-card">
          <div class="binding-detected-head">
            <span>QEMU Guest Agent 已连接</span><el-tag type="success" size="small" effect="plain">实时识别</el-tag>
          </div>
          <div class="binding-detected-grid">
            <div>
              <span>操作系统</span><strong>{{ formatGuestOs(guestBinding) }}</strong>
            </div>
            <div>
              <span>真实 IP</span><strong>{{ guestBinding.qga_ip_address || '未获取' }}</strong>
            </div>
          </div>
          <div class="binding-hint">系统将直接使用 QGA 提供的操作系统和真实 IP，当前只需填写登录凭据。</div>
        </div>
        <template v-else>
          <div class="binding-fallback-card">
            <span class="binding-fallback-dot" />QEMU Guest Agent 不可用，将使用手动配置和 PVE 推断结果。
          </div>
          <div class="binding-manual-head"><span>手动连接信息</span><span>QGA 未连接时必填</span></div>
          <el-form-item label="操作系统">
            <div class="binding-os-row">
              <!-- Windows 需 QGA/ostype/探测显式确证;确证不了的一律按 Linux 对待
                   (与设备 OS 同口径的排除法),并标记「推断」。识别全自动:
                   打开窗口与保存前各静默探测一次,无需手动干预。 -->
              <el-tag
                :type="
                  bindingForm.os_system === 'linux'
                    ? 'success'
                    : bindingForm.os_system === 'windows'
                      ? 'warning'
                      : 'info'
                "
                effect="plain"
              >
                {{
                  bindingForm.os_system === 'linux'
                    ? guestBinding?.os_assumed
                      ? 'Linux (SSH, 推断)'
                      : 'Linux (SSH)'
                    : bindingForm.os_system === 'windows'
                      ? 'Windows (WinRM)'
                      : '未识别'
                }}
              </el-tag>
            </div>
            <div v-if="bindingOsDetection" class="binding-hint binding-result">{{ bindingOsDetection.detail }}</div>
          </el-form-item>
          <div class="binding-form-grid">
            <el-form-item label="虚拟机 IP" required>
              <el-input v-model="bindingForm.ip_address" placeholder="例如 192.168.1.20" />
            </el-form-item>
            <el-form-item label="连接端口">
              <el-input
                v-if="bindingForm.os_system === 'windows'"
                v-model.number="bindingForm.winrm_port"
                inputmode="numeric"
                placeholder="WinRM 5985"
              />
              <el-input v-else v-model.number="bindingForm.ssh_port" inputmode="numeric" placeholder="SSH 22" />
            </el-form-item>
          </div>
        </template>
      </section>

      <section class="binding-section">
        <div class="binding-section-head">
          <span class="binding-section-index">02</span>
          <div>
            <h4>登录凭据</h4>
            <p>凭据仅保存到当前虚拟机，不会出现在全局凭据列表中。</p>
          </div>
        </div>
        <div class="binding-form-grid">
          <el-form-item label="登录用户名" required>
            <el-input v-model="bindingForm.username" placeholder="如 root 或 Administrator" autocomplete="username" />
          </el-form-item>
          <el-form-item label="登录密码" required>
            <el-input
              v-model="bindingForm.password"
              type="password"
              show-password
              placeholder="留空则保留已保存密码"
              autocomplete="current-password"
            />
          </el-form-item>
        </div>
        <el-form-item v-if="bindingForm.os_system !== 'windows'" label="SSH 私钥（可选）">
          <el-input v-model="bindingForm.ssh_key" type="textarea" :rows="3" placeholder="留空则保留已保存私钥" />
        </el-form-item>
      </section>

      <section class="binding-section binding-enable-section">
        <div>
          <h4>启用运维接入</h4>
          <p>关闭后将不会被自动化运维任务使用。</p>
        </div>
        <el-switch v-model="bindingForm.enabled" inline-prompt active-text="启用" inactive-text="停用" />
      </section>
    </el-form>
    <template #footer>
      <span class="binding-footer-hint">保存后可在详情页点击“测试连接”验证凭据。</span>
      <el-button @click="bindingVisible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="bindingSaving"
        :disabled="
          (!bindingForm.ip_address.trim() && !guestBinding?.qga_available) ||
          !bindingForm.username.trim() ||
          (bindingForm.os_system === 'windows'
            ? !bindingForm.password && !guestBinding?.has_password
            : !bindingForm.password &&
              !bindingForm.ssh_key &&
              !guestBinding?.has_password &&
              !guestBinding?.has_ssh_key)
        "
        @click="saveGuestBinding"
        >保存配置</el-button
      >
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
// PVE 虚拟机详情抽屉 + 运维接入配置弹窗。状态与动作全部留在 PvePanel
// (usePveGuestDetail 工厂实例 + 电源/控制台/删除)，经单 prop ctx 传入，
// setup 解构后模板与拆分前 1:1(v-model 写穿回面板状态)。
import { computed, defineAsyncComponent } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useMetricColor } from '@/composables/useMetricColor'
import {
  cpuPct,
  diskPct,
  filesystemSourceLabel,
  fmtBytes,
  fmtDate,
  fmtDiskBytes,
  fmtRate,
  formatGuestOs,
  isTemplate,
  key,
  memPct,
  sortedDisks,
} from '@/utils/pveFormat'
import type { GuestDetailCtx } from './guestDetailCtx'

const props = defineProps<{ ctx: GuestDetailCtx }>()

const {
  detailVisible,
  detail,
  detailLoading,
  onDetailClosed,
  guestBinding,
  bindingLoading,
  bindingTestLoading,
  winrmScriptLoading,
  openBindingForm,
  testGuestAccess,
  downloadGuestWinrmScript,
  bindingVisible,
  bindingForm,
  bindingSaving,
  bindingOsDetection,
  saveGuestBinding,
  snapshots,
  historyRange,
  historyPoints,
  historyLoading,
  historyError,
  loadHistory,
  snapName,
  snapBusy,
  createSnap,
  rollbackSnap,
  deleteSnap,
  openConfigForm,
  power,
  powerBusy,
  openConsole,
  openDelete,
} = props.ctx

// 虚拟化域统一以 pve:* 为准（与面板同口径：settings:manage 不兼任后门）
const authStore = useAuthStore()
const canOperate = computed(() => authStore.hasPermission('pve:manage'))
const canOperateConsole = computed(() => authStore.hasPermission('pve:manage'))
const canConfigureAccess = computed(() => authStore.hasPermission('pve:manage'))

// 虚拟机的 CPU 是相对分配核数、内存是相对 maxmem 的占用率;配色口径与指标页统一。
const metricColor = useMetricColor()
const PveHistoryChart = defineAsyncComponent(() => import('../PveHistoryChart.vue'))
</script>

<style scoped>
/* ── 详情抽屉 ── */
.g-body {
  padding: 0 var(--dcn-space-1);
}
.g-head {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-3);
}
.g-access-card {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-4);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-section);
}
.g-access-status-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--dcn-space-2);
}
.g-access-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--dcn-space-2);
}
.g-access-detail {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: var(--dcn-space-2) var(--dcn-space-3);
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-card);
}
.g-access-detail span {
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}
.g-access-detail strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.g-access-summary {
  min-width: 0;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
  overflow-wrap: anywhere;
}
.g-access-summary-block {
  line-height: 1.5;
}
.g-access-actions {
  display: flex;
  gap: var(--dcn-space-2);
  flex-shrink: 0;
}
.g-access-error {
  width: 100%;
  color: var(--dcn-danger);
  font-size: var(--dcn-text-xs);
  line-height: 1.5;
}
.g-winrm-tools {
  display: flex;
  width: 100%;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-1);
}
.g-winrm-tools .binding-hint {
  margin-top: 0;
}
.binding-dialog :deep(.el-dialog__body) {
  padding-top: 8px;
}
.binding-dialog :deep(.el-dialog__footer) {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.binding-intro {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3) var(--dcn-space-4);
  margin-bottom: var(--dcn-space-4);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: linear-gradient(
    135deg,
    color-mix(in srgb, var(--dcn-primary) 10%, var(--dcn-bg-section)),
    var(--dcn-bg-section)
  );
}
.binding-intro-mark {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 12px;
  background: var(--dcn-primary);
  color: #fff;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}
.binding-intro-title {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
  font-weight: 700;
}
.binding-intro-sub {
  margin-top: 3px;
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.binding-form {
  padding-top: 0;
}
.binding-section {
  padding: var(--dcn-space-4);
  margin-bottom: var(--dcn-space-3);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
}
.binding-section-head {
  display: flex;
  align-items: flex-start;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.binding-section-index {
  color: var(--dcn-primary);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}
.binding-section h4 {
  margin: 0;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-sm);
  font-weight: 700;
}
.binding-section p {
  margin: 3px 0 0;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
  line-height: 1.45;
}
.binding-detected-card {
  padding: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
  border: 1px solid color-mix(in srgb, var(--dcn-success) 35%, var(--dcn-border));
  border-radius: var(--dcn-radius-sm);
  background: color-mix(in srgb, var(--dcn-success) 6%, var(--dcn-bg-card));
}
.binding-detected-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-2);
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-xs);
  font-weight: 600;
}
.binding-detected-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-3);
}
.binding-detected-grid div {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.binding-detected-grid span {
  color: var(--dcn-text-placeholder);
  font-size: 11px;
}
.binding-detected-grid strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.binding-fallback-card {
  padding: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
  border: 1px dashed var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.binding-fallback-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 7px;
  border-radius: 50%;
  background: var(--dcn-text-placeholder);
}
.binding-manual-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: -4px 0 var(--dcn-space-3);
  color: var(--dcn-text-secondary);
  font-size: var(--dcn-text-xs);
}
.binding-manual-head span:last-child {
  color: var(--dcn-text-placeholder);
  font-size: 11px;
}
.binding-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 var(--dcn-space-4);
}
.binding-os-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-3);
}
.binding-result {
  padding: var(--dcn-space-2) var(--dcn-space-3);
  border-radius: var(--dcn-radius-sm);
  background: var(--dcn-bg-section);
}
.binding-enable-section {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-3);
  margin-bottom: 0;
}
.binding-footer-hint {
  margin-right: auto;
  color: var(--dcn-text-placeholder);
  font-size: 11px;
}
.binding-hint {
  margin-top: 5px;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
  line-height: 1.5;
}
@media (max-width: 560px) {
  :deep(.el-drawer) {
    width: 92vw !important;
  }
  .g-access-grid {
    grid-template-columns: 1fr;
  }
  .g-disk-list {
    grid-template-columns: 1fr;
    padding-left: 0;
  }
  .g-winrm-tools {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--dcn-space-2);
  }
  .binding-dialog {
    width: 94vw !important;
  }
  .binding-form-grid,
  .binding-detected-grid {
    grid-template-columns: 1fr;
  }
  .binding-os-row {
    align-items: flex-start;
    flex-direction: column;
  }
  .binding-footer-hint {
    display: none;
  }
}
.g-sub {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
}
.g-section-title {
  font-size: var(--dcn-text-md);
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin: var(--dcn-space-3) 0 var(--dcn-space-2);
}
.g-template-note {
  display: block;
  margin-bottom: var(--dcn-space-2);
}
.g-actions {
  display: flex;
  gap: var(--dcn-space-2);
  flex-wrap: wrap;
}
.g-metrics {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}
.g-metric-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
}
.g-metric-label {
  width: 40px;
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-secondary);
  flex-shrink: 0;
}
.g-bar {
  flex: 1;
}
.g-mem-detail {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
  padding-left: 48px;
}
.g-disk-val {
  font-size: var(--dcn-text-sm);
  color: var(--dcn-text-primary);
  font-variant-numeric: tabular-nums;
}
.g-disk-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--dcn-space-2);
  margin: var(--dcn-space-1) 0;
  padding-left: 48px;
}
.g-disk-card {
  min-width: 0;
  padding: var(--dcn-space-3);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
}
.g-disk-card-head,
.g-disk-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-2);
}
.g-disk-card-head {
  margin-bottom: var(--dcn-space-2);
}
.g-disk-mount {
  min-width: 0;
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.g-disk-card-head strong {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-sm);
  font-variant-numeric: tabular-nums;
}
.g-disk-card-foot {
  margin-top: var(--dcn-space-2);
  color: var(--dcn-text-placeholder);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.throughput-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--dcn-space-2);
}
.throughput-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: var(--dcn-space-3);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
}
.throughput-card span,
.throughput-card small {
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
}
.throughput-card strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-md);
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.throughput-note {
  margin: var(--dcn-space-2) 0 0;
  color: var(--dcn-text-placeholder);
  font-size: var(--dcn-text-xs);
  line-height: 1.5;
}
.g-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-3);
  margin-top: var(--dcn-space-3);
}
.g-section-head .g-section-title {
  margin-top: 0;
}
.history-range {
  width: 126px;
}
.history-shell {
  min-height: 164px;
  border-radius: var(--dcn-radius-lg);
}
.history-shell :deep(.el-empty) {
  min-height: 164px;
  padding: var(--dcn-space-3);
}

.g-snap-actions {
  display: flex;
  gap: var(--dcn-space-2);
  margin-bottom: var(--dcn-space-2);
}
.g-snap-input {
  flex: 1;
}
.g-snaps {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}
.g-snap {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--dcn-space-2);
  padding: var(--dcn-space-2) var(--dcn-space-3);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-sm);
}
.g-snap-name {
  font-weight: 600;
  color: var(--dcn-text-primary);
  margin-right: var(--dcn-space-2);
}
.g-snap-time {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-secondary);
}
.g-snap-desc {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}
.g-snap-actions {
  display: flex;
  flex-shrink: 0;
}
</style>
