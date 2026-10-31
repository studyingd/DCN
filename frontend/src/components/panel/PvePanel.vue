<template>
  <div class="inline-panel">
    <div class="inline-panel-header">
      <h2 class="inline-panel-title">虚拟化管理</h2>
      <span class="pve-desc">Proxmox VE 平台 · 虚拟机 / 容器 全生命周期管理</span>
    </div>

    <el-alert v-if="pollError" :title="pollError" type="warning" show-icon :closable="false" class="poll-alert" />
    <div class="pve-body">
      <!-- 连接选择栏 -->
      <div class="pve-conn-bar">
        <el-select v-model="activeConnId" placeholder="选择 PVE 平台" class="pve-conn-select" @change="onConnChange">
          <el-option
            v-for="c in connections"
            :key="c.id"
            :label="`${c.name} (${c.host}:${c.port})`"
            :value="c.id"
            :disabled="!c.enabled"
          />
        </el-select>
        <el-button :loading="loading" @click="refreshAll"
          ><el-icon><Refresh /></el-icon>刷新</el-button
        >
        <span class="pve-updated" :class="{ 'is-stale': isStale(lastLoaded) }">
          {{ lastLoaded ? `数据时间 ${fmtClock(lastLoaded)}` : '暂无数据' }}
          <el-tag v-if="isStale(lastLoaded)" size="small" type="warning" effect="plain">数据可能已过期</el-tag>
        </span>
        <el-button v-if="canManageConn" type="primary" :disabled="!activeConnId" @click="openCreateGuest">
          <el-icon><Plus /></el-icon>新建虚拟机
        </el-button>
        <div class="pve-conn-bar-right">
          <el-button v-if="canManageConn" plain @click="connMgrVisible = true">
            <el-icon><Setting /></el-icon>管理连接
          </el-button>
        </div>
      </div>

      <!-- 无连接提示 -->
      <el-empty v-if="connections.length === 0 && !connLoading" description="还没有 PVE 平台连接">
        <el-button v-if="canManageConn" type="primary" @click="openConnForm(null)">添加 PVE 连接</el-button>
        <template v-else>请联系管理员在「管理连接」中添加 PVE 平台连接(API Token)。</template>
      </el-empty>

      <template v-else-if="activeConnId">
        <!-- 汇总 -->
        <div class="pve-summary">
          <div class="sum-item">
            <span class="sum-val">{{ guests.length }}</span>
            <span class="sum-label">虚拟机/容器</span>
          </div>
          <div class="sum-item">
            <span class="sum-val sum-running">{{ runningCount }}</span>
            <span class="sum-label">运行中</span>
          </div>
          <div class="sum-item">
            <span class="sum-val">{{ stoppedCount }}</span>
            <span class="sum-label">已停止</span>
          </div>
          <div class="sum-item">
            <span class="sum-val">{{ nodes.length }}</span>
            <span class="sum-label">当前平台节点</span>
          </div>
          <div class="sum-item">
            <span class="sum-val">{{ totalNodeCount }}</span>
            <span class="sum-label">节点总数</span>
            <span class="sum-sub">跨 {{ enabledConnectionCount }} 个 PVE 平台</span>
          </div>
        </div>

        <!-- 虚机表格 -->
        <el-table v-loading="loading" :data="filteredGuests" stripe size="small" class="pve-table">
          <el-table-column prop="vmid" label="VMID" width="86" align="center" sortable />
          <el-table-column prop="name" label="名称" width="150" show-overflow-tooltip sortable>
            <template #default="{ row }">
              <div class="guest-name">
                <span
                  class="status-dot"
                  :class="row.status === 'running' ? 'dot-online' : 'dot-offline'"
                  :title="row.status"
                />
                <span>{{ row.name || `VM ${row.vmid}` }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="64" align="center">
            <template #default="{ row }">
              <el-tag v-if="isTemplate(row)" size="small" type="info" effect="plain">模板</el-tag>
              <el-tag v-else size="small" type="primary" effect="plain">VM</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="CPU" min-width="120">
            <template #default="{ row }">
              <div class="resource-cell">
                <div class="resource-value">
                  <span>{{ cpuPct(row) }}%</span><small>{{ row.maxcpu || row.cpus || '-' }} 核</small>
                </div>
                <el-progress
                  :percentage="cpuPct(row)"
                  :stroke-width="5"
                  :show-text="false"
                  :color="metricColor(cpuPct(row), 'cpu')"
                />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="内存" min-width="158">
            <template #default="{ row }">
              <div class="resource-cell">
                <div class="resource-value">
                  <span>{{ memPct(row) }}%</span><small>{{ fmtBytes(row.mem) }} / {{ fmtBytes(row.maxmem) }}</small>
                </div>
                <el-progress
                  :percentage="memPct(row)"
                  :stroke-width="5"
                  :show-text="false"
                  :color="metricColor(memPct(row), 'memory')"
                />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="磁盘容量" width="88" align="center">
            <template #default="{ row }">{{ fmtBytes(row.maxdisk) }}</template>
          </el-table-column>
          <el-table-column label="磁盘 IO 读/写" width="166" align="center" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="isTemplate(row) || row.status !== 'running'" class="rate-text is-idle">-</span>
              <span v-else class="rate-text">
                {{ fmtRate(row.disk_read_rate, row.disk_rate_unavailable) }} /
                {{ fmtRate(row.disk_write_rate, row.disk_rate_unavailable) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="网络 ↓/↑" width="166" align="center" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="isTemplate(row) || row.status !== 'running'" class="rate-text is-idle">-</span>
              <span v-else class="rate-text">
                {{ fmtRate(row.net_in_rate, row.net_rate_unavailable) }} /
                {{ fmtRate(row.net_out_rate, row.net_rate_unavailable) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="运行时长" width="94" align="right">
            <template #default="{ row }">{{ fmtUptime(row.uptime) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="78" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'running' ? 'success' : 'info'" effect="plain">
                {{ row.status === 'running' ? '运行中' : row.status === 'paused' ? '已暂停' : '已停止' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="174" fixed="right" align="center" class-name="action-column">
            <template #default="{ row }">
              <el-button
                v-if="canOperate && !isTemplate(row) && row.status !== 'running'"
                link
                type="success"
                size="small"
                :loading="powerBusy === key(row, 'start')"
                @click="power(row, 'start')"
                >启动</el-button
              >
              <el-button
                v-if="canOperate && !isTemplate(row) && row.status === 'running'"
                link
                type="danger"
                size="small"
                :loading="powerBusy === key(row, 'stop')"
                @click="power(row, 'stop')"
                >停止</el-button
              >
              <el-button
                v-if="canOperate && !isTemplate(row) && row.status === 'running'"
                link
                type="warning"
                size="small"
                :loading="powerBusy === key(row, 'reboot')"
                @click="power(row, 'reboot')"
                >重启</el-button
              >
              <el-button v-if="canOperate && canClone(row)" link type="primary" size="small" @click="openClone(row)"
                >克隆</el-button
              >
              <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="该 PVE 下没有虚拟机/容器" :image-size="60" /></template>
        </el-table>
      </template>
    </div>

    <!-- ══════════ 虚机详情抽屉 + 运维接入弹窗（状态经 ctx 单 prop 注入） ══════════ -->
    <GuestDetailDrawer :ctx="detailCtx" />

    <!-- ══════════ 新建虚拟机弹窗 ══════════ -->
    <el-dialog v-model="createVisible" title="新建虚拟机 / 容器" width="560px" append-to-body>
      <el-form label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" placeholder="如:web-server-01(仅小写字母/数字/连字符,不支持中文)" />
        </el-form-item>
        <el-form-item label="VMID">
          <el-input-number
            v-model="createForm.vmid"
            :controls="false"
            :min="100"
            :max="999999999"
            placeholder="留空自动分配"
          />
        </el-form-item>
        <el-form-item label="CPU 核数">
          <el-input-number v-model="createForm.cores" :controls="false" :min="1" :max="128" />
        </el-form-item>
        <el-form-item label="内存 (MB)">
          <el-input-number v-model="createForm.memory_mb" :controls="false" :min="512" :step="512" />
        </el-form-item>
        <el-form-item label="磁盘 (GB)">
          <el-input-number v-model="createForm.disk_gb" :controls="false" :min="1" :max="4096" />
        </el-form-item>
        <el-form-item label="存储">
          <el-select
            v-model="createForm.storage"
            filterable
            allow-create
            :loading="storageLoading"
            placeholder="local-lvm"
            style="width: 100%"
          >
            <el-option v-for="st in storageList" :key="st.storage" :label="st.storage" :value="st.storage" />
          </el-select>
          <div class="create-form-hint">磁盘卷将创建在所选存储上（仅列出支持镜像的存储）</div>
        </el-form-item>
        <el-form-item label="ISO 镜像">
          <el-select v-model="createForm.iso" placeholder="选择 ISO(可选)" clearable filterable style="width: 100%">
            <el-option v-for="iso in isoList" :key="iso.volid" :label="iso.volid" :value="iso.volid" />
          </el-select>
        </el-form-item>
        <el-form-item label="QGA">
          <div class="qga-switch-row">
            <el-switch v-model="createForm.agent" />
            <span class="create-form-hint"
              >QEMU Guest Agent（推荐开启）：IP / 操作系统 / 文件系统自动识别、磁盘告警采集都依赖它；需在客户机内安装
              qemu-guest-agent</span
            >
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" @click="saveCreateGuest">创建</el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 连接管理弹窗 ══════════ -->
    <el-dialog v-model="connMgrVisible" title="PVE 平台连接管理" width="740px" append-to-body>
      <div class="conn-mgr">
        <div class="conn-mgr-actions">
          <el-button v-if="canManageConn" type="primary" size="small" @click="openConnForm(null)">
            <el-icon><Plus /></el-icon>添加连接
          </el-button>
        </div>
        <el-table v-loading="connLoading" :data="connections" size="small" stripe>
          <el-table-column prop="name" label="名称" min-width="110" show-overflow-tooltip />
          <el-table-column label="地址" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">{{ row.host }}:{{ row.port }}</template>
          </el-table-column>
          <el-table-column prop="token_id" label="Token ID" min-width="140" show-overflow-tooltip />
          <el-table-column label="启用" width="70" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'" effect="plain">{{
                row.enabled ? '启用' : '停用'
              }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canManageConn" label="操作" width="170" align="center">
            <template #default="{ row }">
              <el-button link type="success" size="small" :loading="testBusyId === row.id" @click="testConn(row)"
                >测试</el-button
              >
              <el-button link type="primary" size="small" @click="openConnForm(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="removeConn(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>

    <!-- ══════════ 连接编辑弹窗 ══════════ -->
    <el-dialog
      v-model="connFormVisible"
      :title="connEditing ? '编辑连接' : '添加 PVE 连接'"
      width="560px"
      append-to-body
      @closed="onConnFormClosed"
    >
      <el-form label-width="132px" class="pve-conn-form">
        <el-form-item label="名称" required>
          <el-input v-model="connForm.name" placeholder="如:生产 PVE" />
        </el-form-item>
        <el-form-item label="主机" required>
          <el-input v-model="connForm.host" placeholder="192.168.1.10(不带 http://)" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input-number v-model="connForm.port" :controls="false" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item label="Token ID" required>
          <el-input v-model="connForm.token_id" placeholder="root@pam!dcn" />
        </el-form-item>
        <el-form-item label="Token Secret" :required="!connEditing">
          <el-input
            v-model="connForm.token_secret"
            type="password"
            show-password
            :placeholder="connEditing ? '留空则不修改' : 'PVEAPIToken 的 secret 部分'"
          />
        </el-form-item>
        <el-form-item label="校验 SSL 证书">
          <el-switch v-model="connForm.verifySslBool" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="connForm.enabledBool" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="connForm.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="connFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="connSaving" @click="saveConn">保存</el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 克隆弹窗 ══════════ -->
    <el-dialog
      v-model="cloneVisible"
      :title="cloneTarget ? `克隆 — ${cloneTarget.name}` : '克隆'"
      width="480px"
      append-to-body
    >
      <el-form label-width="110px">
        <el-form-item label="源">
          <el-text type="info"
            >QEMU 虚拟机 · VMID {{ cloneTarget?.vmid
            }}<template v-if="isTemplate(cloneTarget)">(模板)</template></el-text
          >
        </el-form-item>
        <el-form-item label="新 VMID">
          <el-input-number
            v-model="cloneForm.newid"
            :controls="false"
            :min="100"
            :max="999999999"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="新名称" required>
          <el-input v-model="cloneForm.name" placeholder="新虚机/容器名称(仅小写字母/数字/连字符,不支持中文)" />
        </el-form-item>
        <el-form-item label="完整克隆">
          <el-switch v-model="cloneForm.full" />
          <span class="clone-hint">完整克隆独立于源(推荐);关闭为链接克隆(省空间但依赖源)</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cloneVisible = false">取消</el-button>
        <el-button type="primary" :loading="cloneSaving" @click="saveClone">开始克隆</el-button>
      </template>
    </el-dialog>

    <!-- ══════════ 调整配置弹窗(变更对比,仅关机状态) ══════════ -->
    <el-dialog
      v-model="configVisible"
      :title="detail ? `调整配置 — ${detail.name || `VM ${detail.vmid}`}` : '调整配置'"
      width="520px"
      append-to-body
      class="pve-config-dialog"
    >
      <div class="cfg-rows">
        <div class="cfg-row" :class="{ 'is-changed': configChanged.cores }">
          <div class="cfg-label">CPU 核数</div>
          <div class="cfg-current">{{ configForm.currentCores }}</div>
          <el-icon class="cfg-arrow"><ArrowRight /></el-icon>
          <el-input-number
            v-model="configForm.cores"
            :controls="false"
            :min="1"
            :max="128"
            class="cfg-input"
            aria-label="新 CPU 核数"
          />
        </div>
        <div class="cfg-row" :class="{ 'is-changed': configChanged.memory }">
          <div class="cfg-label">内存</div>
          <div class="cfg-current">{{ configForm.currentMemoryMb }} MB</div>
          <el-icon class="cfg-arrow"><ArrowRight /></el-icon>
          <el-input-number
            v-model="configForm.memory_mb"
            :controls="false"
            :min="512"
            :step="512"
            class="cfg-input"
            aria-label="新内存(MB)"
          />
        </div>
        <div class="cfg-row cfg-row-disk" :class="{ 'is-changed': configChanged.disk }">
          <div class="cfg-label">磁盘</div>
          <div class="cfg-current">{{ currentDiskGb }} GB</div>
          <el-icon class="cfg-arrow"><ArrowRight /></el-icon>
          <el-input-number
            v-model="configForm.disk_gb"
            :controls="false"
            :min="currentDiskGb"
            :max="4096"
            class="cfg-input"
            aria-label="新磁盘容量(GB)"
          />
          <el-tag v-if="configChanged.disk" type="success" size="small" effect="light" class="cfg-delta"
            >+{{ diskDeltaGb }} GB</el-tag
          >
        </div>
      </div>
      <div class="cfg-hints">
        <div class="cfg-hint">
          <el-icon><InfoFilled /></el-icon>
          <span>磁盘只能扩大不能缩小；扩容后需在客户机内扩展文件系统才能使用新空间</span>
        </div>
        <div class="cfg-hint">
          <el-icon><InfoFilled /></el-icon>
          <span>配置在启动虚机后生效</span>
        </div>
      </div>
      <template #footer>
        <span class="cfg-footer-summary">
          {{ hasConfigChange ? configSummaryText : '' }}
        </span>
        <el-button @click="configVisible = false">取消</el-button>
        <el-button type="primary" :loading="configSaving" :disabled="!hasConfigChange" @click="saveGuestConfig"
          >保存调整</el-button
        >
      </template>
    </el-dialog>

    <!-- 删除虚机：先展示会被清理/保留的卷，避免误以为“删了但盘还在” -->
    <el-dialog
      v-model="deleteVisible"
      :title="deleteTarget ? `删除虚拟机 — ${deleteTarget.name}` : '删除虚拟机'"
      width="600px"
      append-to-body
    >
      <div v-loading="deletePreviewLoading" class="del-body">
        <el-alert type="error" :closable="false" show-icon class="del-alert">
          <template #title>此操作不可恢复</template>
          <div class="del-alert-text">
            将销毁 VMID {{ deleteTarget?.vmid }} 的配置、快照，并删除它拥有的磁盘卷。
            <template v-if="deletePreview?.template"
              >该虚机是<b>模板</b>，链接克隆依赖它的磁盘，删除后这些克隆将无法启动。</template
            >
          </div>
        </el-alert>

        <div class="del-section-title">存储清理范围</div>
        <div v-if="deleteVolumes.length === 0" class="del-empty">该虚机没有引用任何磁盘卷。</div>
        <div v-for="vol in deleteVolumes" :key="vol.key" class="del-vol">
          <el-tag :type="volTagType(vol.kind)" size="small" effect="plain">{{ volKindLabel(vol.kind) }}</el-tag>
          <span class="del-vol-key">{{ vol.key }}</span>
          <span class="del-vol-id" :title="vol.volid">{{ vol.volid }}</span>
          <span class="del-vol-size">{{ vol.size_bytes ? fmtBytes(vol.size_bytes) : '-' }}</span>
        </div>
        <div v-if="deletePreserved.length" class="del-hint">
          ISO 安装介质不是虚机私有数据，PVE 销毁虚机时不会删除，需要请到存储自行清理。
        </div>

        <div class="del-section-title">平台侧关联记录</div>
        <div class="del-local">
          <span>运维接入配置 {{ deletePreview?.purged_local.bindings || 0 }} 项</span>
          <span>容器快照 {{ deletePreview?.purged_local.containers || 0 }} 条</span>
          <span>业务关联 {{ deletePreview?.purged_local.business_links || 0 }} 条</span>
        </div>
        <div class="del-hint">
          这些记录会一并清除，否则容器采集和业务监控会继续去连一台已不存在的虚机，产生“永远离线”的假告警。
        </div>

        <div class="del-options">
          <el-checkbox v-model="deletePurge">purge：删除虚机拥有的磁盘卷与快照</el-checkbox>
          <el-checkbox v-model="deleteOrphans">同时销毁 unused 残留卷</el-checkbox>
          <el-checkbox v-if="deletePreview?.running" v-model="deleteForce">
            强制删除（虚机正在运行，先强制停机再销毁）
          </el-checkbox>
        </div>
        <div v-if="deletePreview?.running && !deleteForce" class="del-hint del-hint-warn">
          虚机当前正在运行。PVE 不允许销毁运行中的虚机，需先关机或勾选「强制删除」。
        </div>
      </div>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button
          type="danger"
          :loading="deleteSaving"
          :disabled="deletePreviewLoading || (deletePreview?.running && !deleteForce)"
          @click="confirmDelete"
        >
          确认删除
        </el-button>
      </template>
    </el-dialog>

    <!-- PVE 异步任务进度(创建/克隆提交后轮询到终态) -->
    <el-dialog v-model="taskVisible" title="PVE 任务" width="420px" append-to-body class="pve-task-dialog">
      <div class="pve-task">
        <div v-if="taskPhase === 'running'" class="pve-task-row">
          <el-icon class="is-loading pve-task-spinner"><Loading /></el-icon>
          <div class="pve-task-text">
            <div class="pve-task-title">{{ taskTitle }}</div>
            <div class="pve-task-sub">任务执行中… {{ taskElapsed }}s</div>
          </div>
        </div>
        <div v-else-if="taskPhase === 'ok'" class="pve-task-row">
          <el-icon class="pve-task-ok"><CircleCheckFilled /></el-icon>
          <div class="pve-task-text">
            <div class="pve-task-title">{{ taskTitle }} 已完成</div>
          </div>
        </div>
        <div v-else class="pve-task-row">
          <el-icon class="pve-task-err"><CircleCloseFilled /></el-icon>
          <div class="pve-task-text">
            <div class="pve-task-title">{{ taskTitle }} 失败</div>
            <div class="pve-task-exit">{{ taskDetail }}</div>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="closeTaskDialog">
          {{ taskPhase === 'running' ? '后台运行' : '关闭' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- PVE 虚拟机控制台(noVNC) -->
    <PveConsole ref="consoleRef" v-bind="consoleTarget" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { usePolling } from '@/composables/usePolling'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Refresh,
  Plus,
  Setting,
  Loading,
  CircleCheckFilled,
  CircleCloseFilled,
  ArrowRight,
  InfoFilled,
} from '@element-plus/icons-vue'
import { pveAPI } from '@/api'
import { useAuthStore } from '@/stores/auth'

// 顶栏全局搜索定位目标：DashboardView 选中虚机后传入 { connectionId, vmid }，
// 本面板驱动连接切换与资源加载，目标出现在列表后打开详情抽屉并回发 focused。
const props = defineProps<{
  focusGuest?: { connectionId: number; vmid: number } | null
}>()
const emit = defineEmits<{ focused: [] }>()

// PVE guest 名(QEMU name / LXC hostname)必须是合法 DNS 名,与后端
// validate_dns_name 同一口径:小写字母/数字开头,仅含小写字母/数字/连字符,≤63。
// 中文、大写、下划线、空格都会被 PVE 拒绝(400 invalid DNS name)。
const DNS_NAME_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/

function validateGuestName(name: string): string | null {
  const value = name.trim()
  if (!value) return '请输入名称'
  if (value.length > 63 || !DNS_NAME_RE.test(value)) {
    return '名称必须是合法 DNS 名:以小写字母或数字开头,仅含小写字母/数字/连字符,不超过 63 字符(不支持中文/大写/下划线/空格)'
  }
  return null
}
import { useMetricColor } from '@/composables/useMetricColor'
import { usePveConnections } from '@/composables/usePveConnections'
import { usePveGuestDetail } from '@/composables/usePveGuestDetail'
import { usePveTaskTracker } from '@/composables/usePveTaskTracker'
import {
  canClone,
  cpuPct,
  fmtBytes,
  fmtClock,
  fmtRate,
  fmtUptime,
  isStale,
  isTemplate,
  key,
  memPct,
} from '@/utils/pveFormat'
import PveConsole from './PveConsole.vue'
import GuestDetailDrawer from './pve/GuestDetailDrawer.vue'
import type { GuestDetailCtx } from './pve/guestDetailCtx'
import type { PveGuest, PveGuestVolume, PveGuestVolumePreview, PveNodeStorage } from '@/types/pve'

const authStore = useAuthStore()
// 虚拟化域统一以 pve:* 为准：settings:manage 不再兼任 PVE 管理后门
// （迁移 0034 已给持有该键的角色补授 pve:manage）。
const canManageConn = computed(() => authStore.hasPermission('pve:manage'))
// 电源/快照/克隆/删除与控制台在后端都是 pve:manage 级操作。
const canOperate = computed(() => authStore.hasPermission('pve:manage'))

// ── 连接 ──
// 连接加载/切换/增删改测已抽到 `usePveConnections`(见文件下方实例化)。

// ── 资源 ──
const nodes = ref<{ node: string; status: string }[]>([])
const guests = ref<PveGuest[]>([])
// guests 列表当前归属的连接（vmid 跨平台不唯一，定位虚机前先确认归属，
// 避免连接切换途中拿旧列表里的同号 vmid 误开详情）
const guestsConnId = ref<number | null>(null)
const loading = ref(false)
const lastLoaded = ref<string | null>(null)
const pollError = ref('')
// (pollTimer 声明已随 usePolling 收编删除,2026-10-31)
const pendingTimers: number[] = []

/** 登记一个延迟 loadAll,随组件卸载一并清理。 */
function scheduleLoadAll(delay: number) {
  pendingTimers.push(window.setTimeout(() => void loadAll(true), delay))
}
let loadAllInFlight = false

// 固定按 VMID 升序展示,避免默认乱序
// 默认按内存使用率降序(2026-10-31 用户拍板,与服务器监控同口径):
// 内存压力最大的虚机在最上;停机虚机 mem=0 → memPct=0 自然沉底,
// 不需要额外的状态两级排序。用户点 VMID/名称列头后由 el-table 接管。
const filteredGuests = computed(() => [...guests.value].sort((a, b) => memPct(b) - memPct(a)))
const runningCount = computed(() => guests.value.filter((g) => g.status === 'running').length)
const stoppedCount = computed(() => guests.value.length - runningCount.value)

// 首轮 RRD 预填:记录已预填过的连接,切换连接后重新预填一次
let rrdPrefilledConnId: number | null = null

async function prefillGuestRatesFromRrd() {
  const connId = activeConnId.value
  if (!connId) return
  const num = (v: unknown): number | undefined =>
    v === null || v === undefined || !Number.isFinite(Number(v)) ? undefined : Number(v)
  try {
    const res = await pveAPI.guestsRrdLatest(connId)
    if (activeConnId.value !== connId) return // 请求期间用户切换了连接,丢弃
    const items = res.data.items || {}
    guests.value = guests.value.map((g) => {
      // 只补仍无速率的行;若后端差分已算出值,不覆盖
      if (g.disk_read_rate !== undefined || g.net_in_rate !== undefined) return g
      const point = items[`${g.type}/${g.vmid}`]
      if (!point) return g
      return {
        ...g,
        disk_read_rate: num(point.diskread),
        disk_write_rate: num(point.diskwrite),
        net_in_rate: num(point.netin),
        net_out_rate: num(point.netout),
      }
    })
  } catch {
    // 预填失败不影响常规轮询,下一周期差值自然接管
  }
}

async function loadAll(quiet = false) {
  const connId = activeConnId.value
  if (!connId || loadAllInFlight) return
  loadAllInFlight = true
  if (!quiet) loading.value = true
  try {
    const res = await pveAPI.overview(connId)
    nodes.value = res.data.nodes.map((n) => ({ node: n.node, status: String(n.status) }))
    // 速率由后端差分(cluster/resources 累计计数器 + 进程内基线),前端只展示:
    // 正常情况首轮即真实速率;null 仅出现在后端冷启动首轮或虚机重启计数器回绕。
    guests.value = res.data.guests
    guestsConnId.value = connId
    if (detail.value) {
      const updated = guests.value.find(
        (guest) => guest.vmid === detail.value?.vmid && guest.type === detail.value?.type,
      )
      if (updated) detail.value = { ...detail.value, ...updated }
    }
    lastLoaded.value = new Date().toISOString()
    pollError.value = ''
    // 后端刚重启、基线为空的那一轮,用 RRD 最新采样(本身就是速率)兜一次底。
    const hasNullRate = guests.value.some(
      (g) => !isTemplate(g) && g.status === 'running' && (g.net_in_rate === undefined || g.net_in_rate === null),
    )
    if (hasNullRate && rrdPrefilledConnId !== activeConnId.value) {
      rrdPrefilledConnId = activeConnId.value
      void prefillGuestRatesFromRrd()
    }
  } catch (e: unknown) {
    pollError.value = 'PVE 数据刷新失败，当前仍显示上一次成功数据，请手动重试'
    const err = e as { response?: { data?: { detail?: string } } }
    if (!quiet) ElMessage.error(err.response?.data?.detail || '加载 PVE 资源失败')
  } finally {
    loading.value = false
    loadAllInFlight = false
  }
}

// ── 连接 / 连接管理 ──
// 连接加载、切换、增删改测全部来自 usePveConnections。loadAll 以回调注入，
// 让连接切换/增删后能触发资源刷新，同时保持「连接」与「资源」两个职责解耦。
// 必须定义在 onMounted 之前（onMounted 回调引用 loadConnections）。
const {
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
} = usePveConnections(loadAll)

// 10s 主轮询统一走 usePolling(2026-10-31 收编):页面隐藏不发请求、恢复可见
// 立即补一轮;用户在查看/编辑抽屉时暂停轮询(避免后台刷新抢占线程和面板连接)
const { start: startPolling, stop: stopPolling } = usePolling(
  () => {
    if (detailVisible.value || bindingVisible.value) return undefined
    return loadAll(true).catch(() => {})
  },
  { intervalMs: 10_000 },
)
onMounted(() => {
  loadConnections()
  startPolling()
})
onUnmounted(() => {
  stopPolling()
  // 卸载后仍排队的 setTimeout(loadAll) 会命中已卸载组件,一并清掉。
  pendingTimers.forEach((t) => window.clearTimeout(t))
  pendingTimers.length = 0
  // 详情抽屉的 detailLoadTimer/detailPollTimer 由 usePveGuestDetail.dispose 统一清理。
  dispose()
  // PVE 任务跟踪轮询同理。
  taskDispose()
})

// ── 指标计算 ──
// 虚拟机的 CPU 是相对分配核数、内存是相对 maxmem 的占用率,与物理机巡检语义不完全等价;
// 这里复用同一套阈值只是近似(逼近分配上限同样意味着风险),配色口径至少与指标页统一。
// 展示格式化与判定纯函数已抽到 `@/utils/pveFormat`(可独立单测)。
const metricColor = useMetricColor()

// ── 电源 ──
const powerBusy = ref<string | null>(null)

async function power(g: PveGuest, action: string, fromDetail = false) {
  const labels: Record<string, string> = {
    start: '启动',
    stop: '停止',
    shutdown: '关机',
    reboot: '重启',
    reset: '重置',
  }
  try {
    await ElMessageBox.confirm(`确定对「${g.name}」执行「${labels[action] || action}」?`, '电源操作', {
      type: 'warning',
    })
  } catch {
    return
  }
  powerBusy.value = key(g, action)
  try {
    await pveAPI.power(activeConnId.value!, g.type, g.vmid, action)
    ElMessage.success(`已${labels[action] || action}「${g.name}」`)
    scheduleLoadAll(1500)
    scheduleLoadAll(4000)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    powerBusy.value = null
  }
  if (fromDetail && detail.value) refreshDetail()
}

// ── 详情 / 快照 ──
// 详情抽屉的运维接入绑定、历史指标、快照增删回滚全部来自 usePveGuestDetail。
// 注入 activeConnId(来自连接簇)与 loadAll(资源簇)；电源/删除等操作后需刷新详情
// 时调用返回的 refreshDetail()。必须定义在 onMounted/onUnmounted 之前（二者引用
// detailVisible/bindingVisible/dispose)。
const {
  detailVisible,
  detailLoading,
  detail,
  guestBinding,
  bindingLoading,
  bindingVisible,
  bindingSaving,
  bindingTestLoading,
  winrmScriptLoading,
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
  openBindingForm,
  saveGuestBinding,
  testGuestAccess,
  downloadGuestWinrmScript,
  loadHistory,
  refreshDetail,
  createSnap,
  rollbackSnap,
  deleteSnap,
} = usePveGuestDetail(activeConnId, loadAll)

// 详情抽屉 + 运维接入弹窗的 ctx(单 prop 契约,见 panel/pve/guestDetailCtx.ts):
// 状态与动作仍归本面板,子组件 setup 解构后模板与拆分前 1:1。
const detailCtx: GuestDetailCtx = {
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
}

// ── 顶栏全局搜索定位 ──
// 请求链路全部异步(连接列表 → 连接切换 → overview 加载),用 watch 驱动直到
// 目标虚机出现在归属连接的列表里再打开详情。lastFocusKey 防止同一目标重复
// 打开；focusRetries 限制重试轮数，几轮仍无则提示放弃（虚机已删/平台不可达）。
let lastFocusKey: string | null = null
let focusRetries = 0

function tryFocusGuest() {
  const target = props.focusGuest
  if (!target) return
  const key = `${target.connectionId}:${target.vmid}`
  if (key === lastFocusKey) return
  if (activeConnId.value !== target.connectionId) {
    const conn = connections.value.find((c) => c.id === target.connectionId)
    if (!conn) {
      // 连接列表尚未加载完成时静默等待（connections 到达会再次触发 watch）
      if (connLoading.value || connections.value.length === 0) return
      lastFocusKey = key
      ElMessage.warning('未找到虚拟机所属的 PVE 平台连接')
      emit('focused')
      return
    }
    activeConnId.value = conn.id
    // loadAll 的 in-flight 守卫可能吞掉并发调用：首次加载在途时这里会 no-op，
    // 由 guests watch 下一轮重试。
    void loadAll(true)
    focusRetries = 0
    return
  }
  if (guestsConnId.value !== target.connectionId) {
    // 当前列表还是旧连接的数据，重试触发一轮目标连接的加载
    focusRetries += 1
    if (focusRetries > 6) {
      lastFocusKey = key
      focusRetries = 0
      ElMessage.warning('未找到该虚拟机（可能已删除或平台不可达）')
      emit('focused')
      return
    }
    scheduleLoadAll(400)
    return
  }
  const guest = guests.value.find((g) => g.vmid === target.vmid && g.type === 'qemu')
  if (guest) {
    lastFocusKey = key
    focusRetries = 0
    openDetail(guest)
    emit('focused')
    return
  }
  focusRetries += 1
  if (focusRetries > 6) {
    lastFocusKey = key
    focusRetries = 0
    ElMessage.warning(`虚拟机 ${target.vmid} 不在当前平台列表中（可能已删除）`)
    emit('focused')
    return
  }
  scheduleLoadAll(400)
}

watch([() => props.focusGuest, connections, activeConnId, guests], () => {
  tryFocusGuest()
})

// ── PVE 异步任务跟踪(创建/克隆提交后轮询终态,失败时把 PVE 的 exitstatus
// 展示给用户——此前这些失败是完全静默的) ──
const {
  taskVisible,
  taskTitle,
  taskPhase,
  taskDetail,
  taskElapsed,
  track: trackTask,
  closeTaskDialog,
  dispose: taskDispose,
} = usePveTaskTracker()

// ── 调整配置弹窗的派生值(磁盘容量展示与变更摘要) ──
const currentDiskGb = computed(() => Math.round(configForm.currentDiskBytes / 1024 ** 3))
const diskDeltaGb = computed(() => Math.max(0, configForm.disk_gb - currentDiskGb.value))
const configSummaryText = computed(() => {
  const parts: string[] = []
  if (configChanged.value.cores) parts.push(`CPU ${configForm.currentCores} → ${configForm.cores} 核`)
  if (configChanged.value.memory) {
    const mb = (v: number) => (v >= 1024 ? `${(v / 1024).toFixed(v % 1024 === 0 ? 0 : 1)}GB` : `${v}MB`)
    parts.push(`内存 ${mb(configForm.currentMemoryMb)} → ${mb(configForm.memory_mb)}`)
  }
  if (configChanged.value.disk) parts.push(`磁盘 +${diskDeltaGb.value}GB`)
  return parts.length ? `将调整：${parts.join('，')}` : ''
})

// ── 删除虚机 ──
const deleteVisible = ref(false)
const deleteTarget = ref<PveGuest | null>(null)
const deletePreview = ref<PveGuestVolumePreview | null>(null)
const deletePreviewLoading = ref(false)
const deleteSaving = ref(false)
const deletePurge = ref(true)
const deleteOrphans = ref(true)
const deleteForce = ref(false)

const deleteVolumes = computed(() => deletePreview.value?.volumes ?? [])
const deletePreserved = computed(() => deleteVolumes.value.filter((vol) => vol.kind === 'cdrom' || vol.kind === 'bind'))

const VOL_KIND_LABELS: Record<PveGuestVolume['kind'], string> = {
  disk: '磁盘卷',
  unused: '残留卷',
  cdrom: 'ISO 介质',
  bind: '绑定挂载',
}

function volKindLabel(kind: PveGuestVolume['kind']) {
  return VOL_KIND_LABELS[kind] ?? kind
}

function volTagType(kind: PveGuestVolume['kind']) {
  if (kind === 'disk') return 'danger'
  if (kind === 'unused') return 'warning'
  return 'info'
}

async function openDelete(g: PveGuest) {
  if (!activeConnId.value) return
  deleteTarget.value = g
  deletePreview.value = null
  deletePurge.value = true
  deleteOrphans.value = true
  deleteForce.value = false
  deleteVisible.value = true
  deletePreviewLoading.value = true
  try {
    const res = await pveAPI.guestVolumes(activeConnId.value, g.type, g.vmid, g.node)
    deletePreview.value = res.data
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '获取磁盘清单失败')
    deleteVisible.value = false
  } finally {
    deletePreviewLoading.value = false
  }
}

async function confirmDelete() {
  const g = deleteTarget.value
  if (!g || !activeConnId.value) return
  deleteSaving.value = true
  try {
    const res = await pveAPI.deleteGuest(activeConnId.value, g.type, g.vmid, {
      purge: deletePurge.value,
      destroyUnreferencedDisks: deleteOrphans.value,
      force: deleteForce.value,
    })
    ElMessage.success(res.data.message || `已删除「${g.name}」`)
    deleteVisible.value = false
    detailVisible.value = false
    loadAll(true)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '删除失败')
  } finally {
    deleteSaving.value = false
  }
}

// ── 克隆虚机/模板 ──
const cloneVisible = ref(false)
const cloneSaving = ref(false)
const cloneTarget = ref<PveGuest | null>(null)
const cloneForm = reactive({
  newid: undefined as number | undefined,
  name: '',
  full: true,
})

async function openClone(g: PveGuest) {
  cloneTarget.value = g
  // 源名若含大写/中文/下划线,-copy 默认值本身就不合法;规整成 DNS 名,
  // 规整不出来就退回 vm 前缀,让用户改。
  const base = `${g.name}-copy`
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
  cloneForm.name = base && /^[a-z0-9]/.test(base) ? base : `vm-${g.vmid}-copy`
  cloneForm.full = true
  cloneVisible.value = true
  try {
    const res = await pveAPI.nextId(activeConnId.value!)
    cloneForm.newid = res.data.vmid
  } catch {
    cloneForm.newid = undefined
  }
}

async function saveClone() {
  if (!cloneTarget.value) return
  const nameError = validateGuestName(cloneForm.name)
  if (nameError) {
    ElMessage.warning(nameError)
    return
  }
  cloneSaving.value = true
  try {
    const res = await pveAPI.cloneGuest(activeConnId.value!, cloneTarget.value.type, cloneTarget.value.vmid, {
      newid: cloneForm.newid,
      name: cloneForm.name.trim(),
      full: cloneForm.full,
    })
    ElMessage.success(`已开始克隆「${cloneTarget.value.name}」→ VMID ${res.data.newid}`)
    cloneVisible.value = false
    // 克隆在 PVE 侧异步执行(大磁盘/链接克隆可能几十秒),失败(存储满等)
    // 只有跟踪任务终态才能发现;成功后再刷新列表。
    scheduleLoadAll(2000)
    if (res.data.task && res.data.node) {
      trackTask({
        connId: activeConnId.value!,
        node: res.data.node,
        upid: res.data.task,
        title: `克隆 ${cloneTarget.value.name} → VMID ${res.data.newid}`,
        onSuccess: () => scheduleLoadAll(1500),
      })
    } else {
      scheduleLoadAll(3000)
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '克隆失败')
  } finally {
    cloneSaving.value = false
  }
}

// ── 控制台(noVNC) ──
const consoleRef = ref<InstanceType<typeof PveConsole> | null>(null)
const consoleTarget = reactive({
  connId: null as number | null,
  gtype: 'qemu',
  vmid: null as number | null,
  guestName: '',
})

function openConsole(g: PveGuest) {
  consoleTarget.connId = activeConnId.value
  consoleTarget.gtype = g.type
  consoleTarget.vmid = g.vmid
  consoleTarget.guestName = g.name
  nextTick(() => consoleRef.value?.open())
}

// ── 新建虚机 ──
const createVisible = ref(false)
const createSaving = ref(false)
const isoList = ref<{ volid: string }[]>([])
const createForm = reactive({
  name: '',
  vmid: undefined as number | undefined,
  cores: 2,
  memory_mb: 2048,
  disk_gb: 32,
  storage: 'local-lvm',
  iso: '',
  // QGA 默认开:IP/OS/文件系统识别、磁盘告警采集整条链路的开关
  agent: true,
})

// 存储下拉数据源(仅含支持镜像 content 的存储;后端节点列表有 30s 缓存)
const storageList = ref<PveNodeStorage[]>([])
const storageLoading = ref(false)

async function openCreateGuest() {
  Object.assign(createForm, {
    name: '',
    vmid: undefined,
    cores: 2,
    memory_mb: 2048,
    disk_gb: 32,
    storage: 'local-lvm',
    iso: '',
    agent: true,
  })
  createVisible.value = true
  const node = nodes.value[0]?.node || 'pve'
  // 拉 ISO 列表、下一个 vmid、存储列表(下拉默认选第一个镜像存储)
  storageLoading.value = true
  try {
    const [isoRes, idRes, storageRes] = await Promise.all([
      pveAPI.isos(activeConnId.value!, node, 'local'),
      pveAPI.nextId(activeConnId.value!),
      pveAPI.storage(activeConnId.value!, node).catch(() => ({ data: [] as PveNodeStorage[] })),
    ])
    isoList.value = isoRes.data
    createForm.vmid = idRes.data.vmid
    storageList.value = storageRes.data.filter((st) =>
      String(st.content || '')
        .split(',')
        .includes('images'),
    )
    if (storageList.value.length && !storageList.value.some((st) => st.storage === createForm.storage)) {
      createForm.storage = storageList.value[0].storage
    }
  } catch {
    /* optional */
  } finally {
    storageLoading.value = false
  }
}

async function saveCreateGuest() {
  const nameError = validateGuestName(createForm.name)
  if (nameError) {
    ElMessage.warning(nameError)
    return
  }
  createSaving.value = true
  try {
    const res = await pveAPI.createGuest(activeConnId.value!, 'qemu', {
      vmid: createForm.vmid,
      name: createForm.name,
      cores: createForm.cores,
      memory_mb: createForm.memory_mb,
      disk_gb: createForm.disk_gb,
      storage: createForm.storage,
      iso: createForm.iso || undefined,
      agent: createForm.agent,
    })
    ElMessage.success(`已提交创建 VMID ${res.data.vmid}`)
    createVisible.value = false
    // 创建是异步任务,PVE 侧失败(存储满/VMID 冲突)此前完全静默;跟踪终态,
    // 成功后刷新列表。
    scheduleLoadAll(2000)
    if (res.data.task && res.data.node) {
      trackTask({
        connId: activeConnId.value!,
        node: res.data.node,
        upid: res.data.task,
        title: `创建虚拟机 ${createForm.name} (VMID ${res.data.vmid})`,
        onSuccess: () => scheduleLoadAll(1500),
      })
    } else {
      scheduleLoadAll(3000)
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } } }
    ElMessage.error(err.response?.data?.detail || '创建失败')
  } finally {
    createSaving.value = false
  }
}
</script>

<style scoped>
.pve-desc {
  font-size: 13px;
  color: var(--dcn-text-secondary);
}
.pve-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--dcn-space-4) var(--dcn-space-6);
}

.pve-conn-bar {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.pve-conn-select {
  width: 300px;
}
.pve-conn-bar-right {
  margin-left: auto;
}

.pve-summary {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--dcn-space-3);
  margin-bottom: var(--dcn-space-4);
}
.sum-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: var(--dcn-space-3) var(--dcn-space-4);
  background: var(--dcn-bg-card);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
}
.sum-val {
  font-size: 22px;
  font-weight: 700;
  color: var(--dcn-text-primary);
  font-variant-numeric: tabular-nums;
}
.sum-running {
  color: var(--dcn-dot-online);
}
.sum-label {
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}
.sum-sub {
  color: var(--dcn-text-placeholder);
  font-size: 10px;
  line-height: 1.3;
}

.pve-table {
  border: 1px solid var(--dcn-border-light);
  border-radius: var(--dcn-radius-lg);
  overflow: hidden;
}
.pve-table :deep(.el-table__header-wrapper th .cell),
.pve-table :deep(.el-table__fixed-header-wrapper th .cell) {
  text-align: center;
}

@media (max-width: 900px) {
  .pve-body {
    padding: var(--dcn-space-4);
  }
  .pve-conn-bar {
    align-items: stretch;
    flex-wrap: wrap;
  }
  .pve-conn-select {
    width: min(100%, 300px);
  }
  .pve-conn-bar-right {
    margin-left: 0;
  }
  .pve-summary {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (min-width: 901px) and (max-width: 1280px) {
  .pve-summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.dot-online {
  background: var(--dcn-dot-online);
  box-shadow: 0 0 6px var(--dcn-dot-online);
}
.dot-offline {
  background: var(--dcn-dot-offline);
}

.guest-name {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  min-width: 0;
}
.guest-name span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.resource-cell {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-right: 4px;
}
.resource-value {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 5px;
  font-variant-numeric: tabular-nums;
}
.resource-value span {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-xs);
  font-weight: 600;
}
.resource-value small {
  overflow: hidden;
  color: var(--dcn-text-placeholder);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.resource-cell :deep(.el-progress-bar__outer) {
  background: var(--dcn-bg-muted);
}
.rate-text {
  color: var(--dcn-text-primary);
  font-size: var(--dcn-text-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.rate-text.is-idle {
  color: var(--dcn-text-placeholder);
}

.conn-mgr-actions {
  margin-bottom: var(--dcn-space-3);
}

/* 连接表单:标签不换行,避免必填星号被折行甩到最左 */
.pve-conn-form :deep(.el-form-item__label) {
  white-space: nowrap;
}

.clone-hint {
  margin-left: var(--dcn-space-2);
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

.del-body {
  min-height: 160px;
}

.del-alert-text {
  margin-top: var(--dcn-space-1);
  font-size: var(--dcn-text-xs);
  line-height: 1.6;
}

.del-section-title {
  margin: var(--dcn-space-4) 0 var(--dcn-space-2);
  font-size: var(--dcn-text-sm);
  font-weight: 600;
}

.del-empty {
  padding: var(--dcn-space-2) 0;
  font-size: var(--dcn-text-xs);
  color: var(--dcn-text-placeholder);
}

.del-vol {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-2);
  padding: 6px 0;
  font-size: var(--dcn-text-xs);
  border-bottom: 1px dashed var(--dcn-border-light);
}

.del-vol-key {
  flex: 0 0 64px;
  font-family: var(--dcn-font-mono);
}

.del-vol-id {
  flex: 1 1 auto;
  overflow: hidden;
  font-family: var(--dcn-font-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.del-vol-size {
  flex: 0 0 auto;
  color: var(--dcn-text-placeholder);
}

.del-local {
  display: flex;
  flex-wrap: wrap;
  gap: var(--dcn-space-3);
  font-size: var(--dcn-text-xs);
}

.del-hint {
  margin-top: var(--dcn-space-2);
  font-size: var(--dcn-text-xs);
  line-height: 1.6;
  color: var(--dcn-text-placeholder);
}

.del-hint-warn {
  color: var(--el-color-warning);
}

.del-options {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-1);
  margin-top: var(--dcn-space-4);
}

@media (max-width: 600px) {
  .throughput-grid {
    grid-template-columns: 1fr;
  }
  .g-section-head {
    align-items: flex-start;
    flex-direction: column;
  }
  .history-range {
    width: 100%;
  }
}

/* ── PVE 任务进度弹窗 ── */
.pve-task-dialog :deep(.el-dialog__body) {
  padding-top: 8px;
}
.pve-task-row {
  display: flex;
  align-items: flex-start;
  gap: var(--dcn-space-4);
}
.pve-task-spinner {
  font-size: 22px;
  color: var(--dcn-text-secondary);
}
.pve-task-ok {
  font-size: 22px;
  color: var(--dcn-dot-online);
}
.pve-task-err {
  font-size: 22px;
  color: var(--dcn-danger);
}
.pve-task-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--dcn-text-primary);
  line-height: 1.5;
}
.pve-task-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--dcn-text-placeholder);
  font-variant-numeric: tabular-nums;
}
.pve-task-exit {
  margin-top: 4px;
  font-size: 12px;
  color: var(--dcn-danger-text);
  word-break: break-all;
  line-height: 1.5;
}

/* ── 新建虚机表单:存储下拉/QGA 开关提示 ── */
.create-form-hint {
  font-size: 12px;
  color: var(--dcn-text-placeholder);
  line-height: 1.5;
}
.qga-switch-row {
  display: flex;
  align-items: flex-start;
  gap: var(--dcn-space-3);
}
.qga-switch-row .create-form-hint {
  flex: 1;
}

/* ── 调整配置弹窗:变更对比卡 ── */
.cfg-rows {
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-2);
}
.cfg-row {
  display: flex;
  align-items: center;
  gap: var(--dcn-space-3);
  padding: var(--dcn-space-3) var(--dcn-space-4);
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-card);
  transition: border-color 0.2s ease;
}
.cfg-row.is-changed {
  border-color: var(--dcn-primary);
}
.cfg-label {
  width: 64px;
  flex: none;
  font-size: 13px;
  font-weight: 600;
  color: var(--dcn-text-primary);
}
.cfg-current {
  flex: 1;
  font-size: 13px;
  color: var(--dcn-text-secondary);
  font-variant-numeric: tabular-nums;
}
.cfg-row.is-changed .cfg-current {
  color: var(--dcn-text-primary);
}
.cfg-arrow {
  flex: none;
  font-size: 14px;
  color: var(--dcn-text-placeholder);
}
.cfg-input {
  width: 120px;
  flex: none;
}
.cfg-delta {
  flex: none;
}
.cfg-hints {
  margin-top: var(--dcn-space-4);
  display: flex;
  flex-direction: column;
  gap: var(--dcn-space-1);
}
.cfg-hint {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  color: var(--dcn-text-placeholder);
  line-height: 1.5;
}
.cfg-hint .el-icon {
  flex: none;
  margin-top: 1px;
}
.cfg-footer-summary {
  margin-right: auto;
  font-size: 12px;
  color: var(--dcn-text-secondary);
}
</style>
