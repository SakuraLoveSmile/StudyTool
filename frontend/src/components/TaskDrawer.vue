<template>
  <div>
    <!-- Backdrop -->
    <Transition
      enter-active-class="transition-opacity duration-300 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition-opacity duration-200 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isDrawerOpen"
        class="fixed inset-0 bg-sink/40 backdrop-blur-sm z-40"
        @click="closeDrawer"
      />
    </Transition>

    <!-- Drawer Panel -->
    <Transition
      enter-active-class="transform transition duration-300 ease-out"
      enter-from-class="translate-x-full"
      enter-to-class="translate-x-0"
      leave-active-class="transform transition duration-200 ease-in"
      leave-from-class="translate-x-0"
      leave-to-class="translate-x-full"
    >
      <div
        v-if="isDrawerOpen"
        class="fixed inset-y-0 right-0 z-50 w-full max-w-lg md:max-w-xl bg-surface shadow-2xl flex flex-col border-l border-line"
      >
        <!-- Header -->
        <div class="px-5 py-4 border-b border-line flex items-center justify-between bg-raised/70">
          <div class="flex items-center gap-2.5">
            <h3 class="text-base font-semibold text-ink">任务进度与监控</h3>
            <span
              v-if="activeCount > 0"
              class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-accent/15 text-accent animate-pulse"
            >
              {{ activeCount }} 运行中
            </span>
          </div>
          <div class="flex items-center gap-1">
            <button
              type="button"
              class="p-1.5 text-faint hover:text-sub rounded-lg hover:bg-raised transition-colors cursor-pointer"
              title="刷新任务"
              @click="fetchTasks"
            >
              <svg class="w-4 h-4" :class="{ 'animate-spin': isLoading }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>
            <button
              type="button"
              class="p-1.5 text-faint hover:text-sub rounded-lg hover:bg-raised transition-colors cursor-pointer"
              @click="closeDrawer"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        <!-- Segmented Tabs -->
        <div class="px-4 pt-3 pb-2 border-b border-line bg-surface">
          <div class="grid grid-cols-3 gap-1 bg-raised p-1 rounded-lg text-xs font-medium">
            <button
              type="button"
              class="py-1.5 rounded-md transition-all text-center cursor-pointer"
              :class="currentTab === 'active' ? 'bg-surface text-accent shadow-xs font-semibold' : 'text-sub hover:text-ink'"
              @click="currentTab = 'active'"
            >
              运行中 ({{ activeTasks.length }})
            </button>
            <button
              type="button"
              class="py-1.5 rounded-md transition-all text-center cursor-pointer"
              :class="currentTab === 'actionable' ? 'bg-surface text-err shadow-xs font-semibold' : 'text-sub hover:text-ink'"
              @click="currentTab = 'actionable'"
            >
              异常 ({{ actionableTasks.length }})
            </button>
            <button
              type="button"
              class="py-1.5 rounded-md transition-all text-center cursor-pointer"
              :class="currentTab === 'history' ? 'bg-surface text-ink shadow-xs font-semibold' : 'text-sub hover:text-ink'"
              @click="currentTab = 'history'"
            >
              历史 ({{ historyTasks.length }})
            </button>
          </div>
        </div>

        <!-- History Actions Bar -->
        <div
          v-if="currentTab === 'history' && historyTasks.length > 0"
          class="px-4 py-2 border-b border-line bg-raised/50 flex items-center justify-between text-xs"
        >
          <span class="text-sub">已存档 {{ historyTasks.length }} 条记录</span>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="text-accent hover:text-accent font-medium hover:underline cursor-pointer"
              @click="clearCompleted"
            >
              清理已完成
            </button>
            <span class="text-faint">|</span>
            <button
              type="button"
              class="text-sub hover:text-err font-medium hover:underline cursor-pointer"
              @click="showClearAllModal = true"
            >
              清空历史
            </button>
          </div>
        </div>

        <!-- 轮询中断提示 -->
        <div
          v-if="pollFailed"
          class="px-4 py-2 bg-warn/10 border-b border-warn/30 flex items-center gap-2 text-xs text-warn"
        >
          <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
          <span>状态更新中断，正在自动重试…</span>
          <span v-if="lastSyncedAt" class="text-warn ml-auto">
            最后成功更新：{{ lastSyncedClock }}
          </span>
        </div>

        <!-- Content Area -->
        <div class="flex-1 overflow-y-auto p-4 space-y-3">
          <!-- Active Tab -->
          <div v-if="currentTab === 'active'">
            <div v-if="activeTasks.length === 0">
              <EmptyState title="当前没有运行中的任务" description="资料解析、考点提取和学习卡生成任务将在此处实时显示进度。" />
            </div>
            <div v-else class="space-y-3">
              <div
                v-for="task in prioritizeFocused(activeTasks)"
                :key="task.id"
                class="rounded-xl border border-line bg-surface shadow-xs overflow-hidden transition-all"
              >
                <!-- Task Card Summary (Top tier) -->
                <div class="p-3.5 space-y-2.5">
                  <div class="flex items-start justify-between gap-2">
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center gap-2 flex-wrap">
                        <span
                          class="w-2 h-2 rounded-full shrink-0"
                          :class="task.status === 'pending' ? 'bg-amber-500' : 'bg-brand-500 animate-ping'"
                        />
                        <span class="text-sm font-semibold text-ink truncate">
                          {{ taskTypeLabel(task.task_type) }}
                        </span>
                        <span
                          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
                          :class="getStatusBadgeClass(task.status)"
                        >
                          {{ formatStatus(task.status) }}
                        </span>
                        <span
                          v-if="task.progress_detail?.unit"
                          class="text-[10px] text-faint bg-raised px-1.5 py-0.5 rounded font-mono"
                        >
                          单位: {{ formatUnit(task.progress_detail.unit) }}
                        </span>
                      </div>

                      <p class="text-xs text-sub mt-1 leading-relaxed line-clamp-2">
                        {{ task.progress_detail?.currentAction || task.message || '正在处理中...' }}
                      </p>
                    </div>

                    <div class="flex items-center gap-1.5 shrink-0">
                      <button
                        type="button"
                        class="px-2.5 py-1 text-xs font-medium text-err hover:text-err hover:bg-err/10 rounded-lg border border-err/30 transition-colors shrink-0 disabled:opacity-50 cursor-pointer"
                        :disabled="task.status === 'cancel_requested'"
                        @click="cancel(task.id)"
                      >
                        {{ task.status === 'cancel_requested' ? '正在停止...' : '取消' }}
                      </button>
                    </div>
                  </div>

                  <!-- 当前阻塞信息：默认直接展示（并发请求分别列出） -->
                  <div
                    v-if="blockingLines(task).length > 0"
                    class="rounded-lg border px-3 py-2 space-y-1"
                    :class="blockingToneClass(task)"
                  >
                    <div class="text-[10px] font-semibold uppercase tracking-wider opacity-70">
                      当前阻塞 / 并发请求
                    </div>
                    <div
                      v-for="(line, idx) in blockingLines(task)"
                      :key="idx"
                      class="text-[11px] leading-relaxed flex items-start gap-1.5"
                    >
                      <span
                        class="mt-1 w-1.5 h-1.5 rounded-full shrink-0"
                        :class="line.tone === 'warn' ? 'bg-amber-500' : (line.tone === 'error' ? 'bg-rose-500' : 'bg-brand-500 animate-pulse')"
                      />
                      <span>{{ line.text }}</span>
                    </div>
                  </div>

                  <!-- 等待过久 / 心跳中断提示 -->
                  <div v-if="hintsFor(task).length > 0" class="space-y-1">
                    <div
                      v-for="(hint, idx) in hintsFor(task)"
                      :key="'hint' + idx"
                      class="text-[11px] text-warn bg-warn/10 border border-warn/20 rounded-lg px-2.5 py-1.5"
                    >
                      {{ hint }}
                    </div>
                  </div>

                  <!-- Native Progress Element -->
                  <div class="space-y-1">
                    <div class="flex justify-between text-xs text-sub font-medium">
                      <span>
                        <span v-if="task.status === 'pending'">
                          排队第 {{ task.queue_position || 1 }} 位 · 排队 {{ formatDuration(task.queued_seconds) }}
                        </span>
                        <span v-else-if="task.progress_detail?.total">
                          已处理 {{ task.progress_detail.succeeded }}/{{ task.progress_detail.total }} {{ formatUnit(task.progress_detail.unit) }}
                          <template v-if="task.progress_detail.failed > 0">
                            · <span class="text-err">失败 {{ task.progress_detail.failed }}</span>
                          </template>
                          <template v-if="task.progress_detail.skipped > 0">
                            · 跳过 {{ task.progress_detail.skipped }}
                          </template>
                        </span>
                        <span v-else>
                          正在执行中
                        </span>
                      </span>
                      <span>
                        <span v-if="task.status === 'pending'">等待中</span>
                        <span v-else-if="task.progress_detail?.total">{{ task.progress }}%</span>
                        <span v-else>阶段推进中</span>
                      </span>
                    </div>

                    <!-- Native progress element -->
                    <div class="w-full">
                      <progress
                        v-if="task.status === 'pending' || isIndeterminate(task)"
                        class="w-full h-2 rounded-full"
                      />
                      <progress
                        v-else
                        class="w-full h-2 rounded-full"
                        :value="task.progress"
                        max="100"
                      />
                    </div>
                  </div>

                  <!-- Footer Meta & Actions -->
                  <div class="pt-1 text-[11px] text-sub flex items-center justify-between flex-wrap gap-2 border-t border-line">
                    <div class="flex items-center gap-2 font-mono">
                      <span>已耗时: {{ formatDuration(getLiveElapsedSeconds(task)) }}</span>
                      <span v-if="task.progress_detail?.resultCount" class="text-accent font-sans">
                        · 识别结果: {{ task.progress_detail.resultCount }}
                      </span>
                    </div>

                    <div class="flex items-center gap-2">
                      <button
                        v-if="task.task_type === 'extract_knowledge_points' && (task.progress_detail?.resultCount || 0) > 0"
                        type="button"
                        class="text-accent hover:text-accent font-medium flex items-center gap-1 hover:underline cursor-pointer"
                        @click="openCandidatePreview(task)"
                      >
                        预览考点 ({{ task.progress_detail?.resultCount }})
                      </button>

                      <button
                        type="button"
                        class="text-sub hover:text-ink font-medium flex items-center gap-0.5 cursor-pointer"
                        @click="toggleTaskExpansion(task.id)"
                      >
                        <span>{{ isTaskExpanded(task.id) ? '收起详情' : '展开详情' }}</span>
                        <svg
                          class="w-3.5 h-3.5 transform transition-transform"
                          :class="{ 'rotate-180': isTaskExpanded(task.id) }"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
                        </svg>
                      </button>
                    </div>
                  </div>
                </div>

                <!-- Expanded Diagnostic Details (Inner tier - Accordion) -->
                <div
                  v-if="isTaskExpanded(task.id)"
                  class="p-3.5 bg-raised/70 border-t border-line space-y-3.5 text-xs"
                >
                  <!-- Pipeline Stage Stepper -->
                  <div class="space-y-1.5">
                    <div class="text-[11px] font-semibold text-sub uppercase tracking-wider">
                      流水线阶段
                    </div>
                    <div class="grid grid-cols-2 sm:grid-cols-4 gap-1.5 bg-surface p-2 rounded-lg border border-line/80">
                      <div
                        v-for="(stage, idx) in getStagesForTask(task.task_type)"
                        :key="stage.key"
                        class="flex items-center gap-1.5 p-1 rounded"
                      >
                        <span
                          class="w-4 h-4 rounded-full flex items-center justify-center text-[10px] shrink-0 font-bold"
                          :class="getStageStatusClass(task, stage.key, idx)"
                        >
                          <span v-if="isStageDone(task, stage.key, idx)">✓</span>
                          <span v-else-if="isStageActive(task, stage.key)">●</span>
                          <span v-else>○</span>
                        </span>
                        <span
                          class="text-[11px] truncate"
                          :class="isStageActive(task, stage.key) ? 'font-bold text-accent' : 'text-sub'"
                        >
                          {{ stage.label }}
                        </span>
                      </div>
                    </div>
                  </div>

                  <!-- True Units Counts Grid -->
                  <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    <div class="bg-surface p-2 rounded-lg border border-line/80">
                      <div class="text-[10px] text-faint">已成功批次</div>
                      <div class="text-sm font-semibold text-ink">
                        {{ task.progress_detail?.succeeded ?? 0 }} / {{ task.progress_detail?.total ?? '?' }}
                      </div>
                    </div>
                    <div class="bg-surface p-2 rounded-lg border border-line/80">
                      <div class="text-[10px] text-faint">当前活动单元</div>
                      <div class="text-sm font-semibold text-accent truncate">
                        {{ task.progress_detail?.activeUnits?.length ? `第 ${task.progress_detail.activeUnits.join(', ')} 批` : '无活动请求' }}
                      </div>
                    </div>
                    <div class="bg-surface p-2 rounded-lg border border-line/80">
                      <div class="text-[10px] text-faint">失败 / 跳过</div>
                      <div class="text-sm font-semibold">
                        <span :class="(task.progress_detail?.failed || 0) > 0 ? 'text-err' : 'text-ink'">{{ task.progress_detail?.failed ?? 0 }}</span>
                        <span class="text-faint"> / </span>
                        <span class="text-ink">{{ task.progress_detail?.skipped ?? 0 }}</span>
                      </div>
                    </div>
                    <div class="bg-surface p-2 rounded-lg border border-line/80">
                      <div class="text-[10px] text-faint">累计提取成果</div>
                      <div class="text-sm font-semibold text-ok">
                        {{ task.progress_detail?.resultCount ?? 0 }} 个
                      </div>
                    </div>
                  </div>

                  <!-- 旧任务：详细调用状态不可用 -->
                  <div
                    v-if="isLegacyDetail(task)"
                    class="px-3 py-2 rounded-lg bg-raised border border-line text-[11px] text-sub"
                  >
                    旧任务记录：仅保留阶段级进度，详细调用状态不可用。
                  </div>

                  <!-- Per-unit execution statuses (并发请求分别列出) -->
                  <div v-if="(task.progress_detail?.execUnits?.length || 0) > 0" class="space-y-1.5">
                    <div class="text-[11px] font-semibold text-sub uppercase tracking-wider">
                      执行单元状态
                    </div>
                    <div class="bg-surface rounded-lg border border-line/80 divide-y divide-line">
                      <div
                        v-for="unit in visibleUnits(task)"
                        :key="unit.id"
                        class="px-2.5 py-1.5 flex items-start justify-between gap-2"
                      >
                        <div class="min-w-0">
                          <div class="flex items-center gap-1.5 flex-wrap">
                            <span class="font-semibold text-ink">{{ unitKindLabel(unit) }}</span>
                            <span
                              class="px-1.5 py-0.5 rounded text-[10px]"
                              :class="unitStateClass(unit)"
                            >
                              {{ unitStateLabel(unit) }}
                            </span>
                            <span v-if="unit.scope" class="text-sub">{{ unit.scope }}</span>
                          </div>
                          <div v-if="unitRetryText(unit)" class="text-[10px] text-warn mt-0.5">
                            {{ unitRetryText(unit) }}
                          </div>
                          <div v-if="unit.error" class="text-[10px] text-err mt-0.5 break-all">
                            {{ unit.error }}
                          </div>
                        </div>
                        <div class="text-right shrink-0 font-mono text-[10px] text-sub">
                          <div v-if="unitWaitSeconds(unit, nowTick) !== null" class="text-accent font-semibold">
                            {{ unitWaitSeconds(unit, nowTick) }} 秒
                          </div>
                          <div v-if="unit.attempt && unit.attempt > 1">尝试 {{ unit.attempt }} 次</div>
                          <div v-if="unit.finishedAt">{{ formatEventTime(unit.finishedAt) }}</div>
                        </div>
                      </div>
                    </div>
                  </div>

                  <!-- Live Request Waiting Indicator -->
                  <div
                    v-if="!isLegacyDetail(task) && task.progress_detail?.requestStartedAt && task.status === 'processing'"
                    class="flex items-center justify-between px-3 py-2 bg-accent/10 border border-accent/20 rounded-lg text-accent"
                  >
                    <div class="flex items-center gap-2">
                      <span class="inline-block w-2 h-2 rounded-full bg-brand-500 animate-ping" />
                      <span>正在调用模型 / OCR 服务...</span>
                    </div>
                    <span class="font-mono font-medium">
                      本次请求已等待：{{ getRequestElapsedSeconds(task) }} 秒
                    </span>
                  </div>

                  <!-- Timing Breakdown Table -->
                  <div v-if="task.timing_breakdown" class="space-y-1.5">
                    <div class="text-[11px] font-semibold text-sub uppercase tracking-wider flex justify-between">
                      <span>耗时明细 (毫秒 / 次数)</span>
                    </div>
                    <div class="bg-surface rounded-lg border border-line/80 overflow-hidden">
                      <table class="w-full text-left text-[11px]">
                        <thead class="bg-raised text-sub border-b border-line">
                          <tr>
                            <th class="py-1 px-2.5">类别</th>
                            <th class="py-1 px-2.5">总耗时</th>
                            <th class="py-1 px-2.5">次数</th>
                            <th class="py-1 px-2.5">平均</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-line">
                          <template v-for="(cat, key) in task.timing_breakdown" :key="key">
                            <tr v-if="cat && cat.count > 0">
                              <td class="py-1 px-2.5 text-ink font-medium">{{ formatTimingCategory(key) }}</td>
                              <td class="py-1 px-2.5 font-mono">{{ Math.round(cat.wall_time_ms) }} ms</td>
                              <td class="py-1 px-2.5 font-mono">{{ cat.count }} 次</td>
                              <td class="py-1 px-2.5 font-mono text-sub">
                                {{ cat.count > 0 ? Math.round(cat.wall_time_ms / cat.count) : 0 }} ms
                              </td>
                            </tr>
                          </template>
                        </tbody>
                      </table>
                    </div>
                  </div>

                  <!-- Live Incremental Event Stream -->
                  <div class="space-y-1.5">
                    <TaskEventLog :events="taskEvents[task.id]" :sync-failed="eventSyncFailed[task.id]" @refresh="fetchTaskEvents(task.id, true)" />
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Actionable Tab (Errors / Partial) -->
          <div v-else-if="currentTab === 'actionable'">
            <div v-if="actionableTasks.length === 0">
              <EmptyState title="没有异常任务" description="系统运行正常，没有执行失败或需要人工介入的任务。" />
            </div>
            <div v-else class="space-y-3">
              <div
                v-for="task in prioritizeFocused(actionableTasks)"
                :key="task.id"
                class="rounded-xl border border-err/30 bg-err/10 overflow-hidden space-y-2.5 p-3.5"
              >
                <div class="flex items-start justify-between gap-2">
                  <div>
                    <span class="text-sm font-semibold text-err">
                      {{ taskTypeLabel(task.task_type) }}
                    </span>
                    <span class="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium bg-err/15 text-err">
                      {{ task.status === 'failed' ? '失败' : '部分完成' }}
                    </span>
                  </div>
                  <div class="flex items-center gap-1">
                    <router-link
                      v-if="task.task_type === 'rebuild_vector_index'"
                      to="/settings"
                      class="px-2.5 py-1 text-xs font-medium bg-surface text-accent hover:bg-accent/10 border border-accent/30 rounded-lg transition-colors cursor-pointer"
                      @click="closeDrawer"
                    >
                      到设置确认费用
                    </router-link>
                    <button
                      v-else
                      type="button"
                      class="px-2.5 py-1 text-xs font-medium bg-surface text-accent hover:bg-accent/10 border border-accent/30 rounded-lg transition-colors cursor-pointer"
                      @click="retry(task.id)"
                    >
                      重试任务
                    </button>
                    <button
                      type="button"
                      class="text-xs text-faint hover:text-sub p-1 hover:bg-surface rounded cursor-pointer"
                      title="移除记录"
                      @click="remove(task.id)"
                    >
                      <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                </div>

                <p class="text-xs text-err bg-surface/80 p-2.5 rounded-lg border border-err/30 font-mono leading-relaxed break-all">
                  {{ task.error_msg || task.message || '未知错误' }}
                </p>
                <p v-if="task.task_type === 'rebuild_vector_index'" class="text-xs text-sub">
                  向量索引重建会调用向量 API，可能产生费用，请到设置页核对当前模型并重新确认。
                </p>
                <p v-if="task.task_type === 'generate_recall_cards'" class="text-xs text-sub">
                  重试会复用已成功知识点，只补做失败项。
                </p>

                <!-- Units diagnostic -->
                <div v-if="task.progress_detail" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-sub">
                  <span>成功: {{ task.progress_detail.succeeded }}{{ task.task_type === 'generate_recall_cards' ? ' 个知识点' : ' 批' }}</span>
                  <span>失败: {{ task.progress_detail.failed }}{{ task.task_type === 'generate_recall_cards' ? ' 个知识点' : ' 批' }}</span>
                  <span v-if="task.progress_detail.skipped > 0">跳过: {{ task.progress_detail.skipped }}{{ task.task_type === 'generate_recall_cards' ? ' 个知识点' : ' 批' }}</span>
                  <span>{{ task.task_type === 'generate_recall_cards' ? '候选卡' : '识别结果' }}: {{ task.progress_detail.resultCount }}{{ task.task_type === 'generate_recall_cards' ? ' 张' : ' 个' }}</span>
                </div>
                <details
                  v-if="task.task_type === 'generate_recall_cards' && recallBatchErrors(task).length"
                  open
                  class="rounded-lg border border-err/30 bg-surface/80 p-2.5 text-xs text-sub"
                >
                  <summary class="cursor-pointer font-medium text-err">失败知识点与原因（{{ recallBatchErrors(task).length }} 项）</summary>
                  <ul class="mt-2 max-h-48 space-y-1.5 overflow-y-auto pl-4 list-disc">
                    <li v-for="(error, index) in recallBatchErrors(task)" :key="`${index}-${error}`" class="break-words">{{ error }}</li>
                  </ul>
                </details>

                <button type="button" class="text-xs text-accent underline" :aria-expanded="isTaskExpanded(task.id)" @click="toggleTaskExpansion(task.id)">{{ isTaskExpanded(task.id) ? '收起日志' : '查看执行日志' }}</button>
                <TaskEventLog v-if="isTaskExpanded(task.id)" :events="taskEvents[task.id]" :sync-failed="eventSyncFailed[task.id]" @refresh="fetchTaskEvents(task.id, true)" />

                <div class="text-[11px] text-faint flex justify-between pt-1 border-t border-err/30">
                  <span>ID: {{ task.id.slice(0, 8) }}</span>
                  <span>{{ formatTime(task.created_at) }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- History Tab -->
          <div v-else-if="currentTab === 'history'">
            <div v-if="historyTasks.length === 0">
              <EmptyState title="暂无历史记录" description="已完成或取消的任务记录将显示在此处。" />
            </div>
            <div v-else class="space-y-2.5">
              <div
                v-for="task in prioritizeFocused(historyTasks)"
                :key="task.id"
                class="p-3 rounded-xl border border-line bg-surface hover:border-line transition-colors flex flex-wrap items-center justify-between gap-3 text-xs"
              >
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class="font-medium text-ink truncate">
                      {{ taskTypeLabel(task.task_type) }}
                    </span>
                    <span
                      class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
                      :class="task.status === 'completed' ? 'bg-ok/10 text-ok border border-ok/30' : 'bg-raised text-sub'"
                    >
                      {{ task.status === 'completed' ? '已完成' : '已取消' }}
                    </span>
                    <span v-if="task.elapsed_seconds" class="text-[10px] text-faint font-mono">
                      耗时: {{ formatDuration(task.elapsed_seconds) }}
                    </span>
                  </div>
                  <p class="text-[11px] text-sub mt-0.5 truncate">
                    {{ task.message || formatTime(task.created_at) }}
                  </p>
                  <button type="button" class="text-xs text-accent underline mt-2" :aria-expanded="isTaskExpanded(task.id)" @click="toggleTaskExpansion(task.id)">{{ isTaskExpanded(task.id) ? '收起日志' : '查看执行日志' }}</button>
                </div>

                <button
                  type="button"
                  class="text-faint hover:text-sub p-1 rounded hover:bg-raised transition-colors cursor-pointer"
                  title="移除记录"
                  @click="remove(task.id)"
                >
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
                <TaskEventLog v-if="isTaskExpanded(task.id)" class="w-full" :events="taskEvents[task.id]" :sync-failed="eventSyncFailed[task.id]" @refresh="fetchTaskEvents(task.id, true)" />
              </div>
            </div>
          </div>
        </div>

        <!-- Floating Undo Banner -->
        <Transition
          enter-active-class="transform transition duration-200 ease-out"
          enter-from-class="translate-y-4 opacity-0"
          enter-to-class="translate-y-0 opacity-100"
          leave-active-class="transform transition duration-150 ease-in"
          leave-from-class="translate-y-0 opacity-100"
          leave-to-class="translate-y-4 opacity-0"
        >
          <div
            v-if="showUndoToast"
            class="m-4 p-3 rounded-xl bg-sink text-white text-xs flex items-center justify-between shadow-lg"
          >
            <span>已清理 {{ lastClearedCount }} 条任务记录</span>
            <button
              type="button"
              class="px-2.5 py-1 bg-brand-600 hover:bg-brand-500 text-white rounded font-medium transition-colors cursor-pointer"
              @click="restoreLastClear"
            >
              撤销
            </button>
          </div>
        </Transition>
      </div>
    </Transition>

    <!-- Clear All Confirmation Modal -->
    <BaseModal
      :show="showClearAllModal"
      title="清空所有历史任务"
      max-width="sm"
      @close="showClearAllModal = false"
    >
      <div class="space-y-3 text-sm text-sub">
        <p>确定要清空所有已完成和已取消的任务历史记录吗？</p>
        <p class="text-xs text-faint bg-raised p-2.5 rounded-lg border border-line">
          此操作仅清理任务历史视图，绝不会删除您的教材资料、题库或考点数据。清理后支持在 10 秒内撤销。
        </p>
      </div>
      <template #footer>
        <button
          type="button"
          class="btn btn-secondary text-xs cursor-pointer"
          @click="showClearAllModal = false"
        >
          取消
        </button>
        <button
          type="button"
          class="btn btn-danger text-xs cursor-pointer"
          @click="confirmClearAll"
        >
          确认清空
        </button>
      </template>
    </BaseModal>

    <!-- Candidate Preview Modal -->
    <BaseModal
      :show="showCandidateModal"
      title="考点候选实时预览"
      max-width="lg"
      @close="showCandidateModal = false"
    >
      <div class="space-y-3 text-sm">
        <div class="flex items-center justify-between text-xs text-sub pb-2 border-b border-line">
          <span>当前批次已入库 {{ previewCandidates.length }} 个考点候选</span>
          <button
            type="button"
            class="text-accent hover:underline cursor-pointer"
            @click="refreshPreviewCandidates"
          >
            刷新列表
          </button>
        </div>

        <div v-if="isPreviewLoading" class="py-8 text-center text-faint">
          <div class="animate-spin inline-block w-6 h-6 border-2 border-accent border-t-transparent rounded-full mb-2" />
          <p class="text-xs">加载考点候选...</p>
        </div>

        <div v-else-if="previewCandidates.length === 0" class="py-8 text-center text-faint text-xs">
          暂无已识别的考点候选
        </div>

        <div v-else class="max-h-96 overflow-y-auto space-y-2 pr-1">
          <div
            v-for="cand in previewCandidates"
            :key="cand.id"
            class="p-2.5 rounded-lg border border-line bg-surface text-xs space-y-1"
          >
            <div class="flex items-center justify-between font-semibold text-ink">
              <span class="text-sm">{{ cand.name }}</span>
              <span
                class="px-1.5 py-0.5 rounded text-[10px] font-normal"
                :class="cand.match_type === 'new' ? 'bg-accent/10 text-accent' : 'bg-raised text-sub'"
              >
                {{ cand.match_type === 'new' ? '新考点' : '已有考点' }}
              </span>
            </div>
            <p v-if="cand.definition" class="text-sub text-[11px] leading-relaxed">
              {{ cand.definition }}
            </p>
            <div class="text-[10px] text-faint flex items-center gap-2">
              <span v-if="cand.chapter">{{ cand.chapter }}</span>
              <span>来源依据: {{ cand.citations?.length || 0 }} 处</span>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <button
          type="button"
          class="btn btn-primary text-xs cursor-pointer"
          @click="showCandidateModal = false"
        >
          关闭预览
        </button>
      </template>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue';
import { useTaskManager } from '../composables/useTaskManager';
import { apiClient } from '../api/client';
import type { TaskStatus, ExtractionCandidate, ExecUnitStatus } from '../types';
import {
  blockingLinesFor, hintsForTask, isLegacyDetail, kindLabel,
  taskTypeLabel, unitStateLabel, unitWaitSeconds, activeUnitsOf
} from '../utils/taskStatus';
import { formatDateTime, formatClock, formatDuration, secondsSince } from '../utils/time';
import BaseModal from './BaseModal.vue';
import EmptyState from './EmptyState.vue';
import TaskEventLog from './TaskEventLog.vue';
import { AlertTriangle } from 'lucide-vue-next';

const {
  tasks,
  activeTasks,
  actionableTasks,
  historyTasks,
  activeCount,
  isDrawerOpen,
  isLoading,
  showUndoToast,
  lastClearedCount,
  pollFailed,
  lastSyncedAt,
  eventSyncFailed,
  fetchTasks,
  cancel,
  retry,
  remove,
  clearCompleted,
  restoreLastClear,
  clearAllHistory,
  closeDrawer,
  taskEvents,
  fetchTaskEvents,
  startEventPolling,
  stopEventPolling,
  stopAllEventPolling,
  consumeFocusTaskId
} = useTaskManager();

const currentTab = ref<'active' | 'actionable' | 'history'>('active');
const showClearAllModal = ref(false);
const expandedTaskIds = ref<Set<string>>(new Set());
const focusedTaskId = ref<string | null>(null);

function prioritizeFocused(items: TaskStatus[]): TaskStatus[] {
  const focused = items.find(task => task.id === focusedTaskId.value);
  return focused ? [focused, ...items.filter(task => task.id !== focused.id)] : items;
}

// Candidate Preview Modal
const showCandidateModal = ref(false);
const previewCandidates = ref<ExtractionCandidate[]>([]);
const isPreviewLoading = ref(false);
const currentPreviewTargetId = ref<string | null>(null);

// Live clock ticker
const nowTick = ref(Date.now());
let tickerInterval: any = null;

onMounted(() => {
  fetchTasks();
  tickerInterval = setInterval(() => {
    nowTick.value = Date.now();
  }, 1000);
});

onUnmounted(() => {
  if (tickerInterval) clearInterval(tickerInterval);
  // 关闭详情后停止事件轮询
  stopAllEventPolling();
});

const lastSyncedClock = computed(() => lastSyncedAt.value ? formatClock(new Date(lastSyncedAt.value).toISOString()) : '');

function focusTaskTab(status?: string) {
  currentTab.value = status === 'failed' || status === 'partial' ? 'actionable'
    : status === 'completed' || status === 'cancelled' ? 'history' : 'active';
}

// Open the requested task's current category, including completed/failed tasks.
watch(isDrawerOpen, (open) => {
  if (open) {
    const focusId = consumeFocusTaskId();
    if (focusId) {
      focusedTaskId.value = focusId;
      focusTaskTab(tasks.value.find(task => task.id === focusId)?.status);
      if (!expandedTaskIds.value.has(focusId)) {
        expandedTaskIds.value.add(focusId);
        fetchTaskEvents(focusId, true);
      }
      startEventPolling(focusId);
    }
  } else {
    focusedTaskId.value = null;
    stopAllEventPolling();
    expandedTaskIds.value.clear();
  }
});
watch(() => tasks.value.find(task => task.id === focusedTaskId.value)?.status, status => {
  if (isDrawerOpen.value && focusedTaskId.value && status) focusTaskTab(status);
});

const isTaskExpanded = (id: string) => expandedTaskIds.value.has(id);

const toggleTaskExpansion = async (id: string) => {
  if (expandedTaskIds.value.has(id)) {
    expandedTaskIds.value.delete(id);
    // 收起后停止该任务的事件轮询
    stopEventPolling(id);
  } else {
    expandedTaskIds.value.add(id);
    await fetchTaskEvents(id, true);
    // 展开后持续增量刷新事件
    startEventPolling(id);
  }
};

const confirmClearAll = async () => {
  showClearAllModal.value = false;
  await clearAllHistory();
};

const openCandidatePreview = async (task: TaskStatus) => {
  if (!task.target_id) return;
  currentPreviewTargetId.value = task.target_id;
  showCandidateModal.value = true;
  await refreshPreviewCandidates();
};

const refreshPreviewCandidates = async () => {
  if (!currentPreviewTargetId.value) return;
  try {
    isPreviewLoading.value = true;
    previewCandidates.value = await apiClient.getExtractionCandidates(currentPreviewTargetId.value);
  } catch (err) {
    console.error('Failed to load candidate preview:', err);
  } finally {
    isPreviewLoading.value = false;
  }
};

const formatStatus = (status: string): string => {
  const map: Record<string, string> = {
    pending: '等待队列',
    processing: '执行中',
    completed: '已完成',
    failed: '失败',
    partial: '部分完成',
    cancel_requested: '正在停止...',
    cancelled: '已取消'
  };
  return map[status] || status;
};

const getStatusBadgeClass = (status: string): string => {
  switch (status) {
    case 'processing':
      return 'bg-accent/10 text-accent border border-accent/30';
    case 'pending':
      return 'bg-warn/10 text-warn border border-warn/30';
    case 'completed':
      return 'bg-ok/10 text-ok border border-ok/30';
    case 'partial':
      return 'bg-orange-50 text-orange-700 border border-orange-200';
    case 'failed':
      return 'bg-err/10 text-err border border-err/30';
    default:
      return 'bg-raised text-sub';
  }
};

const formatUnit = (unit?: string): string => {
  if (!unit) return '项';
  const map: Record<string, string> = {
    page: '页',
    chunk: '片段',
    batch: '批次',
    question: '题'
  };
  return map[unit] || unit;
};

const recallBatchErrors = (task: TaskStatus): string[] => {
  if (typeof task.batch_errors_json !== 'string') return [];
  try {
    const value: unknown = JSON.parse(task.batch_errors_json);
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
      : [];
  } catch {
    return [];
  }
};

const isIndeterminate = (task: TaskStatus): boolean => {
  return (
    task.progress_detail?.phase === 'waiting_provider' ||
    task.progress_detail?.phase === 'waiting_api' ||
    task.progress_detail?.phase === 'retry_wait' ||
    !task.progress_detail?.total
  );
};

const blockingLines = (task: TaskStatus) => blockingLinesFor(task, nowTick.value);
const hintsFor = (task: TaskStatus) => hintsForTask(task, nowTick.value);

const blockingToneClass = (task: TaskStatus): string => {
  const lines = blockingLines(task);
  if (lines.some(l => l.tone === 'error')) return 'bg-err/10 border-err/30 text-err';
  if (lines.some(l => l.tone === 'warn')) return 'bg-warn/10 border-warn/30 text-warn';
  return 'bg-accent/10 border-accent/20 text-accent';
};

/** 展开区中的执行单元：活动单元在前，其后是最近结束的单元 */
const visibleUnits = (task: TaskStatus): ExecUnitStatus[] => {
  const units = task.progress_detail?.execUnits || [];
  const active = units.filter(u => activeUnitsOf(task).some(a => a.id === u.id));
  const finished = units.filter(u => !active.includes(u)).slice(-12);
  return [...active, ...finished];
};

const unitKindLabel = (unit: ExecUnitStatus) => kindLabel(unit);
const unitStateClass = (unit: ExecUnitStatus): string => {
  switch (unit.state) {
    case 'awaiting_response':
    case 'waiting_slot':
      return 'bg-accent/10 text-accent';
    case 'retry_wait':
      return 'bg-warn/10 text-warn';
    case 'processing':
    case 'saving':
      return 'bg-accent/10 text-accent';
    case 'succeeded':
      return 'bg-ok/10 text-ok';
    case 'failed':
      return 'bg-err/10 text-err';
    case 'skipped':
      return 'bg-purple-50 text-purple-700';
    case 'cancelled':
      return 'bg-raised text-sub';
    default:
      return 'bg-raised text-sub';
  }
};

const unitRetryText = (unit: ExecUnitStatus): string => {
  if (unit.state !== 'retry_wait') return '';
  let text = `重试等待（第 ${unit.attempt ?? 1} 次尝试）`;
  if (unit.retryWaitSeconds) text += `，约 ${Math.round(unit.retryWaitSeconds)} 秒后重试`;
  if (unit.retryReason) text += `：${unit.retryReason}`;
  return text;
};

const getLiveElapsedSeconds = (task: TaskStatus): number => {
  if (task.status === 'processing' || task.status === 'cancel_requested') {
    const age = secondsSince(task.started_at || task.created_at, nowTick.value);
    if (age !== null) return age;
  }
  return task.elapsed_seconds || 0;
};

const getRequestElapsedSeconds = (task: TaskStatus): number => {
  return secondsSince(task.progress_detail?.requestStartedAt, nowTick.value) || 0;
};

const formatTime = (ts: string): string => formatDateTime(ts);
const formatEventTime = (ts: string): string => formatClock(ts);

const formatTimingCategory = (cat: string): string => {
  const map: Record<string, string> = {
    local_parse: '本地文件解析',
    ocr_request: 'OCR 远程识别',
    llm_extract_request: '模型提取请求',
    embedding_request: '向量模型计算',
    validation: '校验与归一化',
    db_write: '数据库持久化',
    retry_wait: '异常重试等待'
  };
  return map[cat] || cat;
};

// Task pipeline stages
const getStagesForTask = (type: string) => {
  switch (type) {
    case 'rebuild_vector_index':
      return [
        { key: 'read_chunks', label: '读取正文切片' },
        { key: 'generate_embeddings', label: '重新计算向量' },
        { key: 'save_index', label: '校验与替换索引' }
      ];
    case 'generate_library_guide':
      return [
        { key: 'read_sources', label: '读取章节资料' },
        { key: 'llm_generate', label: '模型生成' },
        { key: 'save_guide', label: '校验与保存' }
      ];
    case 'parse_document':
      return [
        { key: 'read_doc', label: '读取文档' },
        { key: 'parse_file', label: '解析 / OCR' },
        { key: 'save_chunks_fts', label: '全文索引' },
        { key: 'generate_embeddings', label: '语义向量' }
      ];
    case 'extract_questions':
      return [
        { key: 'read_chunks', label: '读取切片' },
        { key: 'batch_extract', label: '逐批提取' },
        { key: 'save_questions', label: '试题入库' }
      ];
    case 'extract_knowledge_points':
      return [
        { key: 'read_chunks', label: '读取切片' },
        { key: 'batch_extract', label: '分批梳理' },
        { key: 'save_candidates', label: '考点入库' }
      ];
    case 'generate_card':
      return [
        { key: 'retrieve_context', label: '检索资料' },
        { key: 'llm_generate', label: '模型构建' },
        { key: 'save_card', label: '保存卡片' }
      ];
    case 'generate_recall_cards':
      return [
        { key: 'retrieve_context', label: '检索资料' },
        { key: 'llm_generate', label: '生成回忆卡' },
        { key: 'save_cards', label: '保存卡片' }
      ];
    default:
      return [{ key: 'working', label: '执行中' }];
  }
};

const isStageActive = (task: TaskStatus, stageKey: string): boolean => {
  return task.progress_detail?.stage === stageKey;
};

const isStageDone = (task: TaskStatus, _stageKey: string, stageIdx: number): boolean => {
  const stages = getStagesForTask(task.task_type);
  const currentKey = task.progress_detail?.stage;
  const currentIdx = stages.findIndex(s => s.key === currentKey);
  if (task.status === 'completed') return true;
  if (currentIdx > -1) {
    return stageIdx < currentIdx;
  }
  return false;
};

const getStageStatusClass = (task: TaskStatus, stageKey: string, stageIdx: number): string => {
  if (isStageDone(task, stageKey, stageIdx)) {
    return 'bg-ok/15 text-ok';
  }
  if (isStageActive(task, stageKey)) {
    return 'bg-brand-600 text-white animate-pulse';
  }
  return 'bg-raised text-faint';
};
</script>
