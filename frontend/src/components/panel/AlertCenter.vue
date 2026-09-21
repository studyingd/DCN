<template>
  <div class="inline-panel alert-center">
    <div class="inline-panel-header alert-header">
      <div>
        <h2 class="inline-panel-title">告警中心</h2>
      </div>
      <el-button type="primary" @click="openCreate"
        ><el-icon><Plus /></el-icon>新建告警规则</el-button
      >
    </div>
    <div class="alert-body">
      <div class="stats-grid">
        <div class="stat-card">
          <span>启用规则</span><strong>{{ overview.enabled_rules }}</strong
          ><el-icon><Bell /></el-icon>
        </div>
        <div class="stat-card" :class="{ danger: overview.open_events > 0 }">
          <span>当前未恢复</span><strong>{{ overview.open_events }}</strong
          ><el-icon><Warning /></el-icon>
        </div>
        <div class="stat-card" :class="{ danger: overview.critical_events > 0 }">
          <span>严重告警</span><strong>{{ overview.critical_events }}</strong
          ><el-icon><CircleCloseFilled /></el-icon>
        </div>
        <div class="stat-card">
          <span>通知失败（24h）</span><strong>{{ overview.failed_notifications }}</strong
          ><el-icon><Promotion /></el-icon>
        </div>
      </div>
      <div class="alert-tabs">
        <button :class="{ active: tab === 'rules' }" @click="tab = 'rules'">告警规则</button
        ><button :class="{ active: tab === 'events' }" @click="tab = 'events'">
          告警事件 <el-badge v-if="overview.open_events" :value="overview.open_events" />
        </button>
      </div>
      <section v-if="tab === 'rules'" class="section-card">
        <div class="section-heading">
          <div>
            <strong>告警规则</strong
            ><small
              >覆盖性能、主机、容器和业务状态；自动处置进度与恢复分别推送 alert.remediation / alert.resolved</small
            >
          </div>
          <el-button :loading="loading" @click="loadAll"
            ><el-icon><Refresh /></el-icon>刷新</el-button
          >
        </div>
        <el-table v-loading="loading" :data="rules" row-key="id" class="alert-table">
          <el-table-column label="规则名称" min-width="210">
            <template #default="{ row }"
              ><div class="rule-name-cell">
                <strong>{{ row.name }}</strong
                ><small>规则 #{{ row.id }} · {{ row.enabled ? '实时评估中' : '已暂停评估' }}</small>
              </div></template
            >
          </el-table-column>
          <el-table-column label="告警条件" min-width="190">
            <template #default="{ row }"
              ><div class="condition-cell">
                <span class="condition-dot" :class="severityType(row.severity)"></span
                ><strong>{{ conditionLabel(row) }}</strong>
              </div></template
            >
          </el-table-column>
          <el-table-column
            label="级别"
            width="96"
            align="center"
            header-align="center"
            class-name="severity-column"
            header-class-name="severity-column-header"
            ><template #default="{ row }"
              ><span class="severity-cell"
                ><el-tag :type="severityType(row.severity)" effect="plain" round>{{
                  severityLabel(row.severity)
                }}</el-tag></span
              ></template
            ></el-table-column
          >
          <el-table-column label="监控范围" min-width="145"
            ><template #default="{ row }"
              ><span class="scope-cell"
                ><el-icon><Monitor /></el-icon><span>{{ scopeLabel(row) }}</span></span
              ></template
            ></el-table-column
          >
          <el-table-column label="触发策略" width="142"
            ><template #default="{ row }"
              ><div class="timing-cell">
                <span
                  ><b>{{ row.sustain_seconds }}s</b> 持续</span
                ><span
                  ><b>{{ row.cooldown_seconds }}s</b> 冷却</span
                >
              </div></template
            ></el-table-column
          >
          <el-table-column label="当前告警" width="105" align="center"
            ><template #default="{ row }"
              ><span class="active-count" :class="{ danger: row.active_event_count > 0 }"
                ><el-icon v-if="row.active_event_count > 0"><Warning /></el-icon
                >{{ row.active_event_count > 0 ? row.active_event_count : '无' }}</span
              ></template
            ></el-table-column
          >
          <el-table-column label="状态" width="96"
            ><template #default="{ row }"
              ><div class="status-cell">
                <el-switch v-model="row.enabled" @change="toggleRule(row)" /><small>{{
                  row.enabled ? '已启用' : '已停用'
                }}</small>
              </div></template
            ></el-table-column
          >
          <el-table-column label="操作" width="135" fixed="right"
            ><template #default="{ row }"
              ><el-button link type="primary" @click="openEdit(row)">编辑</el-button
              ><el-button link type="danger" @click="removeRule(row)">删除</el-button></template
            ></el-table-column
          >
        </el-table>
        <el-empty v-if="!loading && rules.length === 0" description="还没有告警规则" />
      </section>
      <section v-else class="section-card">
        <div class="section-heading">
          <div><strong>告警事件</strong><small>查看当前异常与历史恢复记录(支持搜索、时间范围与翻页)</small></div>
          <div class="event-filters">
            <el-select v-model="eventStatus" clearable placeholder="状态" style="width: 100px" @change="onFilterChange"
              ><el-option label="未恢复" value="open" /><el-option label="已恢复" value="resolved" /></el-select
            ><el-select
              v-model="eventSeverity"
              clearable
              placeholder="级别"
              style="width: 100px"
              @change="onFilterChange"
              ><el-option label="信息" value="info" /><el-option label="警告" value="warning" /><el-option
                label="严重"
                value="critical" /></el-select
            ><el-select v-model="eventMetric" clearable placeholder="指标" style="width: 140px" @change="onFilterChange"
              ><el-option label="CPU 使用率" value="cpu_pct" /><el-option
                label="内存使用率"
                value="mem_pct" /><el-option label="磁盘使用率" value="disk_max_pct" /><el-option
                label="主机状态"
                value="host_status" /><el-option label="容器状态" value="container_status" /><el-option
                label="业务状态"
                value="business_status" /></el-select
            ><el-date-picker
              v-model="eventRange"
              type="datetimerange"
              range-separator="至"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
              :shortcuts="rangeShortcuts"
              style="width: 330px"
              @change="onFilterChange"
            />
            <el-input
              v-model="eventSearch"
              clearable
              placeholder="搜索规则/对象/内容"
              :prefix-icon="Search"
              style="width: 180px"
              @input="onSearchInput"
              @clear="onFilterChange"
            />
            <el-button :loading="eventsLoading" @click="fetchEvents()"
              ><el-icon><Refresh /></el-icon>刷新</el-button
            ><el-button link @click="resetEventFilters">重置</el-button
            ><el-button link type="warning" @click="openWindows"
              ><el-icon><Mute /></el-icon>维护静默</el-button
            >
          </div>
        </div>
        <el-table v-loading="eventsLoading" :data="events" row-key="id" class="alert-table event-table">
          <el-table-column label="告警事件" min-width="300"
            ><template #default="{ row }"
              ><div class="event-main">
                <span class="event-severity" :class="severityType(row.severity)"
                  ><el-icon><Warning /></el-icon>{{ severityLabel(row.severity) }}</span
                ><span class="event-copy"
                  ><strong>{{ row.rule_name }}</strong
                  ><small :title="row.message">{{ row.message }}</small></span
                >
              </div></template
            ></el-table-column
          >
          <el-table-column label="对象" min-width="205"
            ><template #default="{ row }"
              ><div class="object-cell">
                <strong>{{ row.device_name }}</strong
                ><small>{{ row.device_ip || row.resource_name || row.resource_type || '-' }}</small>
              </div></template
            ></el-table-column
          >
          <el-table-column label="通知状态" width="110"
            ><template #default="{ row }"
              ><el-tag
                :type="
                  row.notification_status === 'failed'
                    ? 'danger'
                    : row.notification_status === 'sent'
                      ? 'success'
                      : 'info'
                "
                effect="plain"
                round
                >{{ notifyLabel(row.notification_status) }}</el-tag
              ></template
            ></el-table-column
          >
          <el-table-column label="自动处置 / 归因" min-width="250"
            ><template #default="{ row }"
              ><div v-if="canRemediate(row)" class="heal-cell">
                <div class="heal-tag-row">
                  <el-tag :type="remediationTag(row.remediation_state)" effect="plain" round>{{
                    remediationLabel(row.remediation_state)
                  }}</el-tag
                  ><el-button
                    v-if="row.status === 'open'"
                    link
                    type="primary"
                    :loading="healBusy === row.id"
                    @click="remediate(row)"
                    >尝试拉起</el-button
                  >
                </div>
                <small v-if="healHint(row)" :title="row.remediation_detail || ''">{{ healHint(row) }}</small>
              </div>
              <div v-else-if="canAnalyze(row)" class="heal-cell">
                <div class="heal-tag-row">
                  <el-tag :type="analysisTag(row.analysis_state)" effect="plain" round>{{
                    analysisLabel(row.analysis_state)
                  }}</el-tag
                  ><el-button v-if="row.analysis_state" link type="primary" @click="openAnalysis(row)"
                    >查看归因</el-button
                  ><el-button v-else link type="primary" :loading="healBusy === row.id" @click="analyze(row)"
                    >AI 归因</el-button
                  >
                </div>
                <small v-if="row.analysis_summary" :title="row.analysis_summary">{{ row.analysis_summary }}</small>
              </div>
              <span v-else class="heal-none">—</span></template
            ></el-table-column
          >
          <el-table-column label="最近发生" width="156"
            ><template #default="{ row }"
              ><div class="event-time">
                <strong>{{ fmtDate(row.last_seen_at) }}</strong
                ><small>{{ fmtClock(row.last_seen_at) }}</small>
              </div></template
            ></el-table-column
          >
          <el-table-column label="状态" width="132"
            ><template #default="{ row }"
              ><div class="event-status">
                <template v-if="row.status === 'open'"
                  ><el-button
                    v-if="!row.snoozed"
                    link
                    type="warning"
                    :loading="ackBusy === row.id"
                    @click="ackEvent(row)"
                    >已知晓</el-button
                  ><el-tag v-else type="info" effect="plain" round :title="row.snoozed_by_name || '已静音'"
                    >已知晓 · 不再提醒</el-tag
                  > </template
                ><el-tag v-else type="success" effect="plain" round
                  ><span v-if="row.snoozed">已恢复 · 曾已知晓</span><span v-else>已恢复</span></el-tag
                >
              </div></template
            ></el-table-column
          >
        </el-table>
        <el-empty v-if="!eventsLoading && events.length === 0" description="暂无告警事件" />
        <div v-if="eventTotal > 0" class="event-pagination">
          <el-pagination
            v-model:current-page="eventPage"
            v-model:page-size="eventPageSize"
            :total="eventTotal"
            :page-sizes="[20, 50, 100, 200]"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="fetchEvents()"
            @size-change="onPageSizeChange"
          />
        </div>
      </section>
    </div>
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
                <el-checkbox
                  :model-value="isContainerChecked(item)"
                  @click.stop
                  @change="() => toggleContainer(item)"
                />
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
    <el-dialog
      v-model="analysisVisible"
      title="AI 归因分析"
      width="720px"
      class="rule-dialog analysis-dialog"
      :close-on-click-modal="false"
    >
      <div v-loading="analysisLoading" class="analysis-body">
        <div v-if="analysisEvent" class="analysis-meta">
          <strong>{{ analysisEvent.rule_name }}</strong
          ><small
            >{{ analysisEvent.device_name }} · {{ analysisEvent.device_ip || analysisEvent.resource_name || '-' }} ·
            {{ fmtDate(analysisEvent.last_seen_at) }} {{ fmtClock(analysisEvent.last_seen_at) }}</small
          >
        </div>
        <pre class="analysis-text">{{ analysisText || '暂无归因结论，可点击右下角重新分析' }}</pre>
      </div>
      <template #footer
        ><el-button @click="analysisVisible = false">关闭</el-button
        ><el-button type="primary" :loading="healBusy === analysisEvent?.id" @click="rerunAnalysis"
          >重新分析</el-button
        ></template
      >
    </el-dialog>
    <!-- 维护静默窗口:变更前建窗口免轰炸;窗口内事件照常流转,仅通知被抑制 -->
    <el-dialog
      v-model="windowsVisible"
      title="维护静默窗口"
      width="720px"
      class="rule-dialog"
      :close-on-click-modal="false"
    >
      <div class="mw-toolbar">
        <el-button type="primary" size="small" @click="startCreateWindow"
          ><el-icon><Plus /></el-icon>新建窗口</el-button
        >
        <small class="mw-hint">窗口内目标对象的告警通知被抑制(告警照常记录/评估),结束后推送汇总</small>
      </div>
      <el-table v-loading="windowsLoading" :data="windows" row-key="id" size="small" class="alert-table">
        <el-table-column label="窗口" min-width="180"
          ><template #default="{ row }"
            ><div class="rule-name-cell">
              <strong>{{ row.name }}</strong> <small>{{ windowStateLabel(row) }} · {{ row.target_label }}</small>
            </div></template
          ></el-table-column
        >
        <el-table-column label="时段" min-width="230"
          ><template #default="{ row }"
            ><small class="mw-time">{{ fmtDateTime(row.start_at) }} ~ {{ fmtDateTime(row.end_at) }}</small></template
          ></el-table-column
        >
        <el-table-column label="抑制" width="80" align="center"
          ><template #default="{ row }"
            ><span class="mw-muted" :class="{ active: row.muted_count > 0 }">{{ row.muted_count }}</span></template
          ></el-table-column
        >
        <el-table-column label="操作" width="150" fixed="right"
          ><template #default="{ row }"
            ><el-button v-if="row.state !== 'ended'" link type="warning" size="small" @click="finishWindow(row)"
              >提前结束</el-button
            ><el-button link type="danger" size="small" @click="removeWindow(row)">删除</el-button></template
          ></el-table-column
        >
      </el-table>
      <el-empty v-if="!windowsLoading && windows.length === 0" description="还没有静默窗口" :image-size="48" />
      <!-- 新建表单 -->
      <div v-if="windowCreating" class="mw-form">
        <el-form ref="windowFormRef" :model="windowForm" :rules="windowFormRules" label-position="top">
          <el-form-item label="窗口名称" prop="name"
            ><el-input v-model.trim="windowForm.name" placeholder="例如：重启 plm 数据服务器"
          /></el-form-item>
          <div class="form-grid">
            <el-form-item label="开始时间" prop="start_at"
              ><el-date-picker v-model="windowForm.start_at" type="datetime" style="width: 100%" /> ></el-form-item
            >
            <el-form-item label="结束时间" prop="end_at"
              ><el-date-picker v-model="windowForm.end_at" type="datetime" style="width: 100%" /> ></el-form-item
            >
          </div>
          <el-form-item label="静默范围"
            ><div class="target-scope">
              <el-radio-group v-model="windowForm.target_scope">
                <el-radio value="all">全部对象</el-radio>
                <el-radio value="selected">指定对象</el-radio>
              </el-radio-group>
              <el-button v-if="windowForm.target_scope === 'selected'" size="small" @click="windowPickerVisible = true">
                选择（设备与虚拟机 {{ windowForm.target_ids.length }} 台）
              </el-button>
            </div>
            <small class="mw-hint">建议只勾你要变更的对象——范围越小，真正的告警漏报风险越小</small>
          </el-form-item>
        </el-form>
        <div class="mw-form-actions">
          <el-button @click="windowCreating = false">取消</el-button>
          <el-button type="primary" :loading="windowSaving" @click="submitWindow">创建窗口</el-button>
        </div>
      </div>
    </el-dialog>
    <!-- 静默目标选择:复用公共 DeviceSelector -->
    <el-dialog
      v-model="windowPickerVisible"
      title="选择静默对象"
      width="860px"
      append-to-body
      class="rule-dialog"
      :close-on-click-modal="false"
    >
      <DeviceSelector :selected-ids="windowForm.target_ids" @confirm="onWindowSelectorConfirm" />
      <template #footer><el-button type="primary" @click="windowPickerVisible = false">确定</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import {
  Bell,
  Mute,
  Box,
  Briefcase,
  CircleCloseFilled,
  Monitor,
  OfficeBuilding,
  Plus,
  Promotion,
  Refresh,
  Search,
  Warning,
} from '@element-plus/icons-vue'
import { alertAPI } from '@/api'
import DeviceSelector from '@/components/common/DeviceSelector.vue'
import type { AlertEvent, AlertRule, AlertSeverity, MaintenanceWindow } from '@/types'

const tab = ref<'rules' | 'events'>('rules')
const loading = ref(false)
const eventsLoading = ref(false)
const saving = ref(false)
const formVisible = ref(false)
const editing = ref<AlertRule | null>(null)
const formRef = ref<FormInstance>()
const rules = ref<AlertRule[]>([])
const events = ref<AlertEvent[]>([])
const containers = ref<any[]>([])
const businesses = ref<any[]>([])
const eventStatus = ref('open')
const eventSeverity = ref('')
/** 历史事件检索(90 天保留):指标/关键字/时间窗口 + 分页 */
const eventMetric = ref('')
const eventSearch = ref('')
const eventRange = ref<[Date, Date] | null>(null)
const eventPage = ref(1)
const eventPageSize = ref(50)
const eventTotal = ref(0)
const rangeShortcuts = [
  {
    text: '最近 24 小时',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setHours(start.getHours() - 24)
      return [start, end]
    },
  },
  {
    text: '最近 7 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 7)
      return [start, end]
    },
  },
  {
    text: '最近 30 天',
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setDate(start.getDate() - 30)
      return [start, end]
    },
  },
]
const categoryOptions = [
  { value: 'performance', label: '性能指标', description: 'CPU、内存、磁盘使用率', icon: Monitor },
  { value: 'host', label: '主机状态', description: '离线或无法连接', icon: OfficeBuilding },
  { value: 'container', label: '容器状态', description: '退出、Restarting 或未运行', icon: Box },
  { value: 'business', label: '业务监控', description: '服务器或接口异常', icon: Briefcase },
]
const overview = reactive({
  active_rules: 0,
  enabled_rules: 0,
  open_events: 0,
  critical_events: 0,
  events_24h: 0,
  failed_notifications: 0,
})
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
let refreshTimer: number | undefined
const formRules: FormRules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  threshold: [{ required: true, message: '请输入阈值', trigger: 'change' }],
}
// ── 展示文案:后端为唯一真源(/api/alerts/labels),拉取前用本地兜底 ──
// 避免前后端两份字典漂移(曾出现前端多 host_online 等后端没有的键)。
const FALLBACK_METRIC_LABELS: Record<string, string> = {
  cpu_pct: 'CPU',
  mem_pct: '内存',
  disk_max_pct: '磁盘',
  host_status: '主机状态异常',
  container_status: '容器状态异常',
  business_status: '业务状态异常',
}
const FALLBACK_SEVERITY_LABELS: Record<string, string> = { info: '信息', warning: '警告', critical: '严重' }
const FALLBACK_NOTIFY_LABELS: Record<string, string> = {
  sent: '已发送',
  failed: '失败',
  skipped: '跳过',
  pending: '待发送',
}
const FALLBACK_REMEDIATION_LABELS: Record<string, string> = {
  '': '未处置',
  running: '自动处置中',
  verifying: '等待恢复确认',
  succeeded: '处置成功',
  failed: '处置失败',
  skipped: '无法自动处置',
}
const FALLBACK_ANALYSIS_LABELS: Record<string, string> = {
  '': '未分析',
  running: 'AI 归因中',
  completed: 'AI 归因完成',
  failed: 'AI 归因失败',
  skipped: '未启用 Agent',
}
const labelDicts = reactive({
  metrics: FALLBACK_METRIC_LABELS,
  severities: FALLBACK_SEVERITY_LABELS,
  notify_status: FALLBACK_NOTIFY_LABELS,
  remediation_states: FALLBACK_REMEDIATION_LABELS,
  analysis_states: FALLBACK_ANALYSIS_LABELS,
})
/** 模块级缓存:多实例挂载只拉一次 */
let _labelsPromise: Promise<void> | null = null
function loadAlertLabels() {
  if (!_labelsPromise) {
    _labelsPromise = alertAPI
      .labels()
      .then(({ data }) => {
        Object.assign(labelDicts.metrics, data.metrics)
        Object.assign(labelDicts.severities, data.severities)
        Object.assign(labelDicts.notify_status, data.notify_status)
        Object.assign(labelDicts.remediation_states, data.remediation_states)
        Object.assign(labelDicts.analysis_states, data.analysis_states)
      })
      .catch(() => {
        _labelsPromise = null // 失败不缓存,下次挂载重试
      })
  }
  return _labelsPromise
}
const metricLabel = (m: string) => labelDicts.metrics[m] || m
const severityLabel = (s: string) => labelDicts.severities[s] || s
const severityType = (s: string) => (s === 'critical' ? 'danger' : s === 'warning' ? 'warning' : 'info')
const notifyLabel = (s: string) => labelDicts.notify_status[s] || s
const numericMetrics = ['cpu_pct', 'mem_pct', 'disk_max_pct']
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
const conditionLabel = (row: AlertRule) =>
  numericMetrics.includes(row.metric)
    ? `${metricLabel(row.metric)} ${row.operator === 'gte' ? '≥' : '>'} ${row.threshold}%`
    : metricLabel(row.metric)
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
const healMetrics = ['host_status', 'container_status']
const canRemediate = (row: AlertEvent) => healMetrics.includes(row.metric)
const canAnalyze = (row: AlertEvent) => numericMetrics.includes(row.metric)
const healBusy = ref(0)
const targetSelectorVisible = ref(false)
const analysisVisible = ref(false)
const analysisLoading = ref(false)
const analysisEvent = ref<AlertEvent | null>(null)
const analysisText = ref('')
const remediationLabel = (s?: string) => labelDicts.remediation_states[s || ''] || '未处置'
const remediationTag = (s?: string) =>
  s === 'succeeded' ? 'success' : s === 'failed' ? 'danger' : s === 'running' || s === 'verifying' ? 'warning' : 'info'
const analysisLabel = (s?: string) => labelDicts.analysis_states[s || ''] || '未分析'
const analysisTag = (s?: string) =>
  s === 'completed' ? 'success' : s === 'failed' ? 'danger' : s === 'running' ? 'warning' : 'info'
const healHint = (row: AlertEvent) =>
  [row.remediation_target, row.remediation_attempts ? `已尝试 ${row.remediation_attempts} 次` : '']
    .filter(Boolean)
    .join(' · ')
const errText = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback
const scopeLabel = (row: AlertRule) => {
  if (row.metric.startsWith('container_')) {
    const devices = row.target_device_ids?.length || 0
    const containers = row.target_container_ids?.length || 0
    if (!devices && !containers) return '全部容器'
    return containers ? `${devices} 台设备 · ${containers} 个容器` : `${devices} 台设备`
  }
  if (row.metric.startsWith('business_'))
    return row.target_business_ids?.length ? `${row.target_business_ids.length} 个业务` : '全部业务'
  const devices = row.target_device_ids?.length || 0
  if (!devices) return '全部资源'
  const guests = (row.target_device_ids || []).filter((id) => id < 0).length
  return guests ? `${devices - guests} 台设备 · ${guests} 台虚拟机` : `${devices} 台设备`
}
function dateParts(value: string) {
  const date = value ? new Date(value) : null
  return date && !Number.isNaN(date.getTime()) ? date : null
}
const fmtDate = (v: string) => {
  const d = dateParts(v)
  return d
    ? `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`
    : '-'
}
const fmtClock = (v: string) => {
  const d = dateParts(v)
  return d ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}` : '--:--'
}
let loadInFlight: Promise<void> | null = null
async function loadOverview() {
  const o = await alertAPI.overview()
  Object.assign(overview, o.data)
}
async function loadRules() {
  rules.value = (await alertAPI.rules()).data
}
async function loadTargets() {
  const result = await Promise.all([alertAPI.containers(), alertAPI.businesses()])
  containers.value = result[0].data
  businesses.value = result[1].data
  // 设备 / 虚拟机候选由 DeviceSelector 自行加载(机房与虚拟化分组卡片)
}
async function loadAll() {
  if (loadInFlight) return loadInFlight
  loading.value = true
  loadInFlight = (async () => {
    try {
      await Promise.all([loadOverview(), loadRules()])
      if (formVisible.value) await loadTargets()
    } catch {
      ElMessage.error('加载告警中心失败')
    } finally {
      loading.value = false
      loadInFlight = null
    }
  })()
  return loadInFlight
}
async function fetchEvents(quiet = false) {
  if (!quiet) eventsLoading.value = true
  try {
    const params: Record<string, string | number | undefined> = {
      status: eventStatus.value || undefined,
      severity: eventSeverity.value || undefined,
      metric: eventMetric.value || undefined,
      q: eventSearch.value.trim() || undefined,
      page: eventPage.value,
      page_size: eventPageSize.value,
    }
    const range = eventRange.value
    if (Array.isArray(range) && range.length === 2) {
      const [from, to] = range
      if (from) params.since = new Date(from).toISOString()
      if (to) params.until = new Date(to).toISOString()
    }
    const data = (await alertAPI.events(params)).data
    events.value = data.items
    eventTotal.value = data.total
    // 筛选后当前页超出总页数时(如换筛选后仍在第 3 页),回到第 1 页重拉
    const maxPage = Math.max(1, Math.ceil(eventTotal.value / eventPageSize.value))
    if (eventPage.value > maxPage) {
      eventPage.value = maxPage
      await fetchEvents(quiet)
    }
  } catch {
    if (!quiet) ElMessage.error('加载告警事件失败')
  } finally {
    if (!quiet) eventsLoading.value = false
  }
}
/** 筛选条件变化:回到第 1 页重新拉取。 */
function onFilterChange() {
  eventPage.value = 1
  void fetchEvents()
}
/** 关键字搜索防抖 400ms:每个字符都发请求会打爆后端。 */
let searchDebounce: number | undefined
function onSearchInput() {
  if (searchDebounce !== undefined) window.clearTimeout(searchDebounce)
  searchDebounce = window.setTimeout(() => {
    searchDebounce = undefined
    onFilterChange()
  }, 400)
}
function onPageSizeChange() {
  eventPage.value = 1
  void fetchEvents()
}
function resetEventFilters() {
  eventStatus.value = 'open'
  eventSeverity.value = ''
  eventMetric.value = ''
  eventSearch.value = ''
  eventRange.value = null
  onFilterChange()
}
/**
 * 归因轮询统一实现:长耗时任务,列表(盯行状态)/弹窗(拉详情)共用同一时钟。
 * 页面不可见跳过;超过 maxPolls 停;回调返回 false 也停。
 */
function startAnalysisWatch(intervalMs: number, maxPolls: number, onPoll: () => Promise<boolean>): number {
  let polls = 0
  const timer = window.setInterval(async () => {
    if (document.hidden) return
    if (++polls > maxPolls) {
      window.clearInterval(timer)
      analysisTimerIds.delete(timer)
      return
    }
    const keep = await onPoll().catch(() => true)
    if (!keep) {
      window.clearInterval(timer)
      analysisTimerIds.delete(timer)
    }
  }, intervalMs)
  analysisTimerIds.add(timer)
  return timer
}

function stopAllAnalysisWatchers() {
  for (const timer of analysisTimerIds) window.clearInterval(timer)
  analysisTimerIds.clear()
}

const analysisTimerIds = new Set<number>()

/**
 * 列表定向盯梢:结论通常十几秒就出,默认 30s 整体刷新等不了那么久。
 * 每 5s 盯这一条,落到终态就停;迟迟没进入 running(闸门拒绝/提交失败)也不空等。
 */
function startAnalysisListPolling(eventId: number) {
  stopAllAnalysisWatchers()
  startAnalysisWatch(5000, 180, async () => {
    await fetchEvents(true)
    const row = events.value.find((item) => item.id === eventId)
    if (!row) return false
    const state = row.analysis_state || ''
    if (state === 'completed' || state === 'failed' || state === 'skipped') return false
    return state !== ''
  })
}
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
async function openCreate() {
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
  targetSelectorVisible.value = false
  formVisible.value = true
  await loadTargets()
}
async function openEdit(row: AlertRule) {
  editing.value = row
  const targetDeviceIds = row.target_device_ids ? [...row.target_device_ids] : []
  const targetContainerIds = row.target_container_ids ? [...row.target_container_ids] : []
  const targetBusinessIds = row.target_business_ids ? [...row.target_business_ids] : []
  Object.assign(form, {
    ...row,
    category: categoryFor(row.metric),
    severity: row.severity,
    // 目标列表为空 = 服务端语义上的「全部资源」
    target_scope:
      targetDeviceIds.length + targetContainerIds.length + targetBusinessIds.length > 0 ? 'selected' : 'all',
    target_device_ids: targetDeviceIds,
    target_container_ids: targetContainerIds,
    target_business_ids: targetBusinessIds,
  })
  targetSelectorVisible.value = false
  formVisible.value = true
  await loadTargets()
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
    await loadAll()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存告警规则失败')
  } finally {
    saving.value = false
  }
}
async function toggleRule(row: AlertRule) {
  try {
    const { data } = await alertAPI.updateRule(row.id, { enabled: row.enabled })
    const closed = data?.closed_event_count || 0
    if (closed) ElMessage.success(`规则已停用，同时关闭了 ${closed} 条进行中告警`)
    else if (!row.enabled) ElMessage.success('规则已停用')
    await loadAll()
  } catch {
    row.enabled = !row.enabled
    ElMessage.error('更新规则状态失败')
  }
}
async function removeRule(row: AlertRule) {
  try {
    await ElMessageBox.confirm(
      `确定删除“${row.name}”吗？进行中的告警会一并关闭，历史告警记录会保留。`,
      '删除告警规则',
      { type: 'warning' },
    )
    const { data } = await alertAPI.deleteRule(row.id)
    ElMessage.success(data?.message || '告警规则已删除')
    await loadAll()
  } catch {
    /* cancelled */
  }
}
/** 已知晓:恢复前不再重复提醒(评估/归因照常) */
const ackBusy = ref(0)
async function ackEvent(row: AlertEvent) {
  ackBusy.value = row.id
  try {
    await alertAPI.ackEvent(row.id)
    ElMessage.success('已知晓，该告警恢复前不再重复提醒')
    await fetchEvents(true)
  } catch {
    ElMessage.error('操作失败，请稍后重试')
  } finally {
    ackBusy.value = 0
  }
}
async function remediate(row: AlertEvent) {
  healBusy.value = row.id
  try {
    await alertAPI.remediateEvent(row.id)
    ElMessage.success('已下发拉起指令，正在跟踪恢复状态')
    await fetchEvents(true)
  } catch (e) {
    ElMessage.error(errText(e, '自动处置失败'))
  } finally {
    healBusy.value = 0
  }
}
async function analyze(row: AlertEvent) {
  healBusy.value = row.id
  try {
    await alertAPI.analyzeEvent(row.id)
    ElMessage.success('已触发 AI 归因分析，结论出来后会自动刷新')
    await fetchEvents(true)
    startAnalysisListPolling(row.id)
  } catch (e) {
    ElMessage.error(errText(e, 'AI 归因分析失败'))
  } finally {
    healBusy.value = 0
  }
}
/** 归因是长耗时任务:弹窗打开时每 10s 拉一次，最多 15 分钟,页面不可见则跳过 */
function startAnalysisPolling() {
  stopAllAnalysisWatchers()
  startAnalysisWatch(10000, 90, async () => {
    const running = await refreshAnalysis()
    return running
  })
}
/** 拉一次归因详情;返回是否仍在分析中(用于决定要不要继续轮询) */
async function refreshAnalysis() {
  const row = analysisEvent.value
  if (!row) return false
  try {
    const detail = (await alertAPI.event(row.id)).data
    analysisEvent.value = detail
    analysisText.value =
      detail.analysis_text ||
      detail.analysis_summary ||
      (detail.analysis_state === 'running' ? 'AI 归因分析进行中…' : '')
    if (detail.analysis_state !== 'running') {
      stopAllAnalysisWatchers()
      await fetchEvents(true)
    }
    return detail.analysis_state === 'running'
  } catch {
    return false
  }
}
async function openAnalysis(row: AlertEvent) {
  analysisEvent.value = row
  analysisText.value = row.analysis_text || row.analysis_summary || ''
  analysisVisible.value = true
  analysisLoading.value = true
  stopAllAnalysisWatchers()
  const running = await refreshAnalysis()
  analysisLoading.value = false
  if (running) startAnalysisPolling()
}
async function rerunAnalysis() {
  const row = analysisEvent.value
  if (!row) return
  healBusy.value = row.id
  try {
    await alertAPI.analyzeEvent(row.id)
    analysisText.value = 'AI 归因分析进行中…'
    startAnalysisPolling()
    await fetchEvents(true)
  } catch (e) {
    ElMessage.error(errText(e, 'AI 归因分析失败'))
  } finally {
    healBusy.value = 0
  }
}
onMounted(() => {
  void loadAlertLabels()
  void loadAll()
  if (tab.value === 'events') void fetchEvents(true)
  refreshTimer = window.setInterval(() => {
    if (document.hidden) return
    void loadOverview()
    if (tab.value === 'rules') void loadRules()
    else void fetchEvents(true)
  }, 30000)
})
watch(tab, (value) => {
  if (value === 'events') void fetchEvents(true)
  else void Promise.all([loadOverview(), loadRules()])
})
watch(analysisVisible, (open) => {
  if (!open) stopAllAnalysisWatchers()
})
// ---- 维护静默窗口 ----
const windowsVisible = ref(false)
const windowsLoading = ref(false)
const windows = ref<MaintenanceWindow[]>([])
const windowCreating = ref(false)
const windowSaving = ref(false)
const windowPickerVisible = ref(false)
const windowFormRef = ref<FormInstance>()
const windowForm = reactive({
  name: '',
  start_at: new Date(Date.now() - 60_000),
  end_at: new Date(Date.now() + 30 * 60_000),
  target_scope: 'all' as 'all' | 'selected',
  target_ids: [] as number[],
})
const windowFormRules: FormRules = {
  name: [{ required: true, message: '请输入窗口名称', trigger: 'blur' }],
  start_at: [{ required: true, message: '请选择开始时间', trigger: 'change' }],
  end_at: [{ required: true, message: '请选择结束时间', trigger: 'change' }],
}
const fmtDateTime = (v: string) => {
  const d = dateParts(v)
  return d ? `${fmtDate(v)} ${fmtClock(v)}` : '-'
}
const windowStateLabel = (row: MaintenanceWindow) =>
  ({ active: '静默中', pending: '未开始', ended: '已结束' })[row.state] || row.state
async function loadWindows() {
  windowsLoading.value = true
  try {
    windows.value = (await alertAPI.maintenanceWindows()).data
  } catch {
    ElMessage.error('加载静默窗口失败')
  } finally {
    windowsLoading.value = false
  }
}
async function openWindows() {
  windowsVisible.value = true
  windowCreating.value = false
  await loadWindows()
}
function startCreateWindow() {
  Object.assign(windowForm, {
    name: '',
    start_at: new Date(Date.now() - 60_000),
    end_at: new Date(Date.now() + 30 * 60_000),
    target_scope: 'all',
    target_ids: [],
  })
  windowCreating.value = true
}
function onWindowSelectorConfirm(ids: number[]) {
  windowForm.target_ids = ids
}
async function submitWindow() {
  if (!(await windowFormRef.value?.validate().catch(() => false))) return
  if (windowForm.end_at <= windowForm.start_at) {
    ElMessage.error('结束时间必须晚于开始时间')
    return
  }
  windowSaving.value = true
  try {
    await alertAPI.createMaintenanceWindow({
      name: windowForm.name,
      target_ids: windowForm.target_scope === 'selected' && windowForm.target_ids.length ? windowForm.target_ids : null,
      start_at: windowForm.start_at.toISOString(),
      end_at: windowForm.end_at.toISOString(),
    })
    ElMessage.success('静默窗口已创建,窗口内通知将被抑制')
    windowCreating.value = false
    await loadWindows()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    windowSaving.value = false
  }
}
async function finishWindow(row: MaintenanceWindow) {
  try {
    await alertAPI.finishMaintenanceWindow(row.id)
    ElMessage.success('窗口已结束,通知已恢复(汇总稍后推送)')
    await loadWindows()
  } catch {
    ElMessage.error('操作失败')
  }
}
async function removeWindow(row: MaintenanceWindow) {
  try {
    await ElMessageBox.confirm(`删除窗口「${row.name}」？通知立即恢复,不推汇总。`, '删除静默窗口', { type: 'warning' })
    await alertAPI.deleteMaintenanceWindow(row.id)
    ElMessage.success('已删除')
    await loadWindows()
  } catch {
    /* cancelled */
  }
}

onUnmounted(() => {
  window.clearInterval(refreshTimer)
  if (searchDebounce !== undefined) window.clearTimeout(searchDebounce)
  stopAllAnalysisWatchers()
})
</script>

<style scoped>
.alert-center {
  background: var(--dcn-bg-page);
}
.alert-header {
  align-items: center;
}
.alert-header > div:first-child {
  min-width: 0;
}
.alert-header > .el-button {
  align-self: center;
  min-height: 40px;
  padding-inline: 18px;
}
.alert-header p {
  margin: 7px 0 0;
  color: var(--dcn-text-secondary);
  font-size: 13px;
}
.alert-body {
  padding: 0 var(--dcn-space-5) var(--dcn-space-5);
  overflow: auto;
}
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  padding: 4px 0 18px;
}
.stat-card {
  position: relative;
  padding: 16px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-card);
}
.stat-card span {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.stat-card strong {
  display: block;
  margin-top: 10px;
  color: var(--dcn-text-primary);
  font-size: 27px;
}
.stat-card .el-icon {
  position: absolute;
  right: 16px;
  top: 17px;
  color: var(--dcn-primary-light);
  font-size: 20px;
}
.stat-card.danger strong,
.stat-card.danger .el-icon {
  color: var(--dcn-danger);
}
.alert-tabs {
  display: flex;
  gap: 6px;
  border-bottom: 1px solid var(--dcn-border);
  margin-bottom: 16px;
}
.alert-tabs button {
  padding: 11px 16px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--dcn-text-secondary);
  cursor: pointer;
  font: inherit;
}
.alert-tabs button.active {
  border-color: var(--dcn-primary);
  color: var(--dcn-primary);
  font-weight: 600;
}
.section-card {
  padding: 18px;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-lg);
  background: var(--dcn-bg-section);
}
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.section-heading > div:first-child {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.section-heading small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.event-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.event-filters .el-select {
  width: 120px;
}
.event-pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.event-main {
  display: flex;
  align-items: flex-start;
  gap: 9px;
}
.event-main span {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.event-main small,
.cell-sub {
  color: var(--dcn-text-secondary);
  font-size: 12px;
  white-space: normal;
}
.cell-sub {
  display: block;
  margin-top: 3px;
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
@media (max-width: 900px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 600px) {
  .alert-body {
    padding-inline: 12px;
  }
  .stats-grid {
    grid-template-columns: 1fr;
  }
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }
  .event-filters {
    width: 100%;
    flex-wrap: wrap;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
}
.alert-body {
  padding-top: 20px;
}
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
.condition-cell {
  align-items: center;
}
.condition-cell .condition-dot {
  margin-top: 0;
  transform: translateY(1px);
}
.alert-table .condition-cell {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  line-height: 22px;
}
.alert-table .condition-cell strong {
  display: block;
  line-height: 22px;
}
.alert-table .condition-cell .condition-dot {
  display: block;
  margin: 0;
  transform: translateY(0);
}
.alert-table :deep(.el-table__header-wrapper th) {
  height: 44px;
  background: var(--dcn-bg-card);
  color: var(--dcn-text-secondary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.alert-table :deep(.el-table__body tr) {
  transition: background-color 0.16s ease;
}
.alert-table :deep(.el-table__body tr:hover > td) {
  background: color-mix(in srgb, var(--dcn-primary) 5%, var(--dcn-bg-card));
}
.alert-table :deep(.el-table__cell) {
  padding: 12px 0;
}
.rule-name-cell,
.condition-cell,
.object-cell,
.event-copy,
.timing-cell,
.status-cell,
.event-time,
.event-status {
  display: flex;
  flex-direction: column;
}
.rule-name-cell {
  gap: 4px;
}
.rule-name-cell strong,
.condition-cell strong,
.object-cell strong {
  color: var(--dcn-text-primary);
  font-size: 13px;
  font-weight: 650;
}
.rule-name-cell small,
.condition-cell small,
.object-cell small,
.timing-cell span,
.status-cell small,
.event-copy small {
  color: var(--dcn-text-secondary);
  font-size: 11px;
  line-height: 1.45;
  /* 业务的异常明细等消息自带换行,按真实换行渲染(2026-09-18) */
  white-space: pre-line;
}
.condition-cell {
  flex-direction: row;
  align-items: flex-start;
  gap: 9px;
}
.condition-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 8px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--dcn-info);
}
.condition-dot.warning {
  background: var(--dcn-warning);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--dcn-warning) 12%, transparent);
}
.condition-dot.danger {
  background: var(--dcn-danger);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--dcn-danger) 12%, transparent);
}
.scope-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--dcn-text-primary);
  font-size: 12px;
}
.scope-cell .el-icon {
  color: var(--dcn-primary-light);
}
.timing-cell {
  gap: 3px;
}
.timing-cell b {
  color: var(--dcn-text-primary);
  font: 600 12px var(--dcn-font-mono);
}
.active-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 34px;
  color: var(--dcn-text-secondary);
  font: 600 12px var(--dcn-font-mono);
}
.active-count.danger {
  color: var(--dcn-danger);
}
.status-cell {
  align-items: flex-start;
  gap: 3px;
}
.status-cell small {
  font-size: 10px;
}
.event-main {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.event-severity {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  flex: 0 0 auto;
  box-sizing: border-box;
  height: 24px;
  padding: 0 7px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 650;
  line-height: 1;
  vertical-align: middle;
}
.event-main .event-severity {
  flex-direction: row;
  min-width: auto;
}
.event-severity .el-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  margin: 0;
  line-height: 1;
  vertical-align: middle;
}
.event-severity .el-icon svg {
  display: block;
  width: 12px;
  height: 12px;
}
.event-severity.warning {
  background: color-mix(in srgb, var(--dcn-warning) 13%, transparent);
  color: var(--dcn-warning);
}
.event-severity.danger {
  background: color-mix(in srgb, var(--dcn-danger) 13%, transparent);
  color: var(--dcn-danger);
}
.event-severity.info {
  background: color-mix(in srgb, var(--dcn-info) 13%, transparent);
  color: var(--dcn-info);
}
.event-copy {
  gap: 4px;
  min-width: 0;
}
.event-copy strong {
  overflow: hidden;
  color: var(--dcn-text-primary);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.event-copy small {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  white-space: normal;
  -webkit-line-clamp: 2;
}
.object-cell {
  gap: 4px;
}
.event-time {
  align-items: flex-start;
  gap: 2px;
  padding-left: 10px;
  border-left: 2px solid color-mix(in srgb, var(--dcn-primary) 38%, transparent);
}
.event-time strong {
  color: var(--dcn-text-primary);
  font: 600 12px var(--dcn-font-mono);
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.event-time small {
  color: var(--dcn-primary-light);
  font: 600 13px var(--dcn-font-mono);
}
.event-status {
  align-items: flex-start;
}
@media (max-width: 900px) {
  .alert-table :deep(.el-table__body-wrapper) {
    overflow-x: auto;
  }
  .event-time {
    padding-left: 7px;
  }
}
.alert-table .condition-cell {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  line-height: 22px;
}
.alert-table .condition-cell strong {
  display: block;
  line-height: 22px;
}
.alert-table .condition-cell .condition-dot {
  display: block;
  margin: 0;
  transform: none;
}
.alert-table :deep(th.severity-column-header .cell),
.alert-table :deep(td.severity-column .cell) {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  line-height: 24px;
}
.severity-cell {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 54px;
  min-height: 24px;
  line-height: 1;
  vertical-align: middle;
}
.severity-cell :deep(.el-tag) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  margin: 0;
  line-height: 22px;
  vertical-align: middle;
}
.heal-cell {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.heal-tag-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.heal-cell small {
  overflow: hidden;
  color: var(--dcn-text-secondary);
  font-size: 11px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.heal-none {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.heal-tag-row :deep(.el-button.is-link) {
  height: 22px;
  padding: 0;
  font-size: 12px;
}
.analysis-body {
  min-height: 200px;
}
.analysis-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 12px;
}
.analysis-meta strong {
  color: var(--dcn-text-primary);
  font-size: 14px;
}
.analysis-meta small {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.analysis-text {
  max-height: 52vh;
  margin: 0;
  padding: 14px 16px;
  overflow: auto;
  border: 1px solid var(--dcn-border);
  border-radius: var(--dcn-radius-md);
  background: var(--dcn-bg-page);
  color: var(--dcn-text-primary);
  font: 12px/1.75 var(--dcn-font-mono);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>

<style scoped>
.mw-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.mw-hint {
  color: var(--dcn-text-secondary);
  font-size: 12px;
}
.mw-time {
  color: var(--dcn-text-secondary);
  font: 12px var(--dcn-font-mono);
}
.mw-muted {
  color: var(--dcn-text-secondary);
  font: 600 12px var(--dcn-font-mono);
}
.mw-muted.active {
  color: var(--dcn-warning);
}
.mw-form {
  margin-top: 14px;
  padding: 16px;
  border: 1px solid var(--dcn-border);
  border-radius: 12px;
  background: var(--dcn-bg-card);
}
.mw-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
