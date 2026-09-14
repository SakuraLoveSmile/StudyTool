import { ref, computed } from 'vue';
import { apiClient, sessionTransport } from '../api/client';
import type { TaskStatus, TaskEventItem } from '../types';

// Global singleton reactive state
const tasks = ref<TaskStatus[]>([]);
const activeCount = ref(0);
const actionableCount = ref(0);
const historyCount = ref(0);
const isDrawerOpen = ref(false);
const isLoading = ref(false);

// 轮询中断可见性：任务列表获取失败时提示“状态更新中断”和最后成功更新时间
const pollFailed = ref(false);
const lastSyncedAt = ref<number | null>(null);

const taskEvents = ref<Record<string, TaskEventItem[]>>({});
const taskLastEventIds = ref<Record<string, number>>({});
// 事件流同步失败（按任务），成功后清除
const eventSyncFailed = ref<Record<string, boolean>>({});

// 从分析页点击任务摘要时打开抽屉并定位展开该任务
const focusTaskId = ref<string | null>(null);

const lastClearToken = ref<string | null>(null);
const lastClearedCount = ref(0);
const showUndoToast = ref(false);
let undoToastTimeout: any = null;

let pollTimer: any = null;
let isPollingActive = false;

// 展开任务后的增量事件轮询（每 2 秒），任务终态补取最后事件后停止
const EVENT_POLL_INTERVAL = 2000;
const eventPollTimers: Record<string, any> = {};
const eventPollVersions: Record<string, number> = {};

const TERMINAL_STATUSES = ['completed', 'failed', 'partial', 'cancelled'];

export function useTaskManager() {
  const activeTasks = computed(() =>
    tasks.value.filter(t => ['pending', 'processing', 'cancel_requested'].includes(t.status))
  );

  const actionableTasks = computed(() =>
    tasks.value.filter(t => ['failed', 'partial'].includes(t.status))
  );

  const historyTasks = computed(() =>
    tasks.value.filter(t => ['completed', 'cancelled'].includes(t.status))
  );

  const applyHistoryResponse = (res: Awaited<ReturnType<typeof apiClient.getTaskHistory>>) => {
    tasks.value = res.tasks;
    activeCount.value = res.active_count;
    actionableCount.value = res.actionable_count;
    historyCount.value = res.history_count;
  };

  const fetchTasks = async () => {
    const epoch = sessionTransport.epoch;
    try {
      isLoading.value = true;
      const res = await apiClient.getTaskHistory({ limit: 50 });
      applyHistoryResponse(res);
      pollFailed.value = false;
      lastSyncedAt.value = Date.now();

      if (res.active_count > 0) {
        startPolling();
      } else {
        stopPolling();
      }
    } catch (err) {
      if (epoch !== sessionTransport.epoch || sessionTransport.isCancelled(err)) return;
      console.error('Failed to fetch tasks:', err);
      pollFailed.value = true;
    } finally {
      if (epoch === sessionTransport.epoch) isLoading.value = false;
    }
  };

  const scheduleNextPoll = () => {
    const epoch = sessionTransport.epoch;
    if (!isPollingActive) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!isPollingActive) return;
      try {
        const res = await apiClient.getTaskHistory({ limit: 50 });
        applyHistoryResponse(res);
        pollFailed.value = false;
        lastSyncedAt.value = Date.now();

        if (res.active_count === 0) {
          stopPolling();
          return;
        }
      } catch (e) {
        if (epoch !== sessionTransport.epoch || sessionTransport.isCancelled(e)) return;
        console.error('Error during task polling:', e);
        pollFailed.value = true;
      } finally {
        if (isPollingActive && epoch === sessionTransport.epoch) {
          scheduleNextPoll();
        }
      }
    }, 2000);
  };

  const startPolling = () => {
    if (isPollingActive) return;
    isPollingActive = true;
    scheduleNextPoll();
  };

  const stopPolling = () => {
    isPollingActive = false;
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  };

  const fetchTaskEvents = async (taskId: string, reset = false) => {
    const epoch = sessionTransport.epoch;
    try {
      const afterId = reset ? undefined : taskLastEventIds.value[taskId];
      const res = await apiClient.getTaskEvents(taskId, afterId);
      eventSyncFailed.value[taskId] = false;
      if (reset || !taskEvents.value[taskId]) {
        taskEvents.value[taskId] = res.events;
      } else if (res.events && res.events.length > 0) {
        // 增量追加，去重防回放
        const existingIds = new Set(taskEvents.value[taskId].map(e => e.id));
        const newEvents = res.events.filter(e => !existingIds.has(e.id));
        taskEvents.value[taskId] = [...taskEvents.value[taskId], ...newEvents];
      }
      if (res.last_id) {
        taskLastEventIds.value[taskId] = res.last_id;
      }
    } catch (err) {
      if (epoch !== sessionTransport.epoch || sessionTransport.isCancelled(err)) return;
      console.error(`Failed to fetch events for task ${taskId}:`, err);
      eventSyncFailed.value[taskId] = true;
    }
  };

  const stopEventPolling = (taskId: string) => {
    eventPollVersions[taskId] = (eventPollVersions[taskId] ?? 0) + 1;
    if (eventPollTimers[taskId]) {
      clearTimeout(eventPollTimers[taskId]);
      delete eventPollTimers[taskId];
    }
  };

  const startEventPolling = (taskId: string) => {
    if (eventPollTimers[taskId]) return;
    const epoch = sessionTransport.epoch;
    const version = eventPollVersions[taskId] = (eventPollVersions[taskId] ?? 0) + 1;
    const tick = async () => {
      if (epoch !== sessionTransport.epoch || eventPollVersions[taskId] !== version) return;
      delete eventPollTimers[taskId];
      try {
        const task = tasks.value.find(t => t.id === taskId);
        const terminal = !task || TERMINAL_STATUSES.includes(task.status);
        // 增量读取新事件；任务结束时补取最后事件
        await fetchTaskEvents(taskId);
        if (terminal) {
          stopEventPolling(taskId);
          return;
        }
      } catch (e) {
        console.error(`Event polling error for ${taskId}:`, e);
      }
      if (epoch === sessionTransport.epoch && eventPollVersions[taskId] === version) {
        eventPollTimers[taskId] = setTimeout(tick, EVENT_POLL_INTERVAL);
      }
    };
    eventPollTimers[taskId] = setTimeout(tick, EVENT_POLL_INTERVAL);
  };

  const stopAllEventPolling = () => {
    Object.keys(eventPollVersions).forEach(stopEventPolling);
  };

  // Drop all user-specific state when the authenticated tenant changes.
  const reset = () => {
    stopPolling();
    stopAllEventPolling();
    if (undoToastTimeout) clearTimeout(undoToastTimeout);
    tasks.value = [];
    activeCount.value = 0;
    actionableCount.value = 0;
    historyCount.value = 0;
    taskEvents.value = {};
    taskLastEventIds.value = {};
    eventSyncFailed.value = {};
    focusTaskId.value = null;
    lastClearToken.value = null;
    lastClearedCount.value = 0;
    showUndoToast.value = false;
    isDrawerOpen.value = false;
    isLoading.value = false;
    pollFailed.value = false;
    lastSyncedAt.value = null;
  };

  const retry = async (taskId: string) => {
    try {
      await apiClient.retryTask(taskId);
      await fetchTasks();
    } catch (err) {
      console.error(`Failed to retry task ${taskId}:`, err);
    }
  };

  const cancel = async (taskId: string) => {
    try {
      await apiClient.cancelTask(taskId);
      await fetchTasks();
    } catch (err) {
      console.error(`Failed to cancel task ${taskId}:`, err);
    }
  };

  const remove = async (taskId: string) => {
    try {
      await apiClient.deleteTask(taskId);
      await fetchTasks();
    } catch (err) {
      console.error(`Failed to remove task ${taskId}:`, err);
    }
  };

  const clearCompleted = async () => {
    try {
      const res = await apiClient.clearTasks('completed');
      lastClearToken.value = res.clear_token;
      lastClearedCount.value = res.cleared_count;
      showUndoToast.value = true;
      if (undoToastTimeout) clearTimeout(undoToastTimeout);
      undoToastTimeout = setTimeout(() => {
        showUndoToast.value = false;
      }, 10000);
      await fetchTasks();
    } catch (err) {
      console.error('Failed to clear completed tasks:', err);
    }
  };

  const restoreLastClear = async () => {
    if (!lastClearToken.value) return;
    try {
      await apiClient.restoreTasks(lastClearToken.value);
      lastClearToken.value = null;
      showUndoToast.value = false;
      await fetchTasks();
    } catch (err) {
      console.error('Failed to restore tasks:', err);
    }
  };

  const clearAllHistory = async () => {
    try {
      const res = await apiClient.clearTasks('terminal');
      lastClearToken.value = res.clear_token;
      lastClearedCount.value = res.cleared_count;
      showUndoToast.value = true;
      if (undoToastTimeout) clearTimeout(undoToastTimeout);
      undoToastTimeout = setTimeout(() => {
        showUndoToast.value = false;
      }, 10000);
      await fetchTasks();
    } catch (err) {
      console.error('Failed to clear all history:', err);
    }
  };

  const openDrawer = () => {
    isDrawerOpen.value = true;
    fetchTasks();
  };

  const closeDrawer = () => {
    isDrawerOpen.value = false;
    focusTaskId.value = null;
    // 关闭详情后停止事件轮询
    stopAllEventPolling();
  };

  const toggleDrawer = () => {
    isDrawerOpen.value = !isDrawerOpen.value;
    if (isDrawerOpen.value) {
      fetchTasks();
    } else {
      closeDrawer();
    }
  };

  // 分析页任务摘要点击直达：打开抽屉并展开对应任务详情
  const openTaskDetail = async (taskId: string) => {
    focusTaskId.value = taskId;
    isDrawerOpen.value = true;
    await fetchTasks();
  };

  const consumeFocusTaskId = (): string | null => {
    const id = focusTaskId.value;
    focusTaskId.value = null;
    return id;
  };

  return {
    tasks,
    activeTasks,
    actionableTasks,
    historyTasks,
    activeCount,
    actionableCount,
    historyCount,
    isDrawerOpen,
    isLoading,
    showUndoToast,
    lastClearedCount,
    pollFailed,
    lastSyncedAt,
    eventSyncFailed,
    fetchTasks,
    startPolling,
    stopPolling,
    cancel,
    retry,
    remove,
    clearCompleted,
    restoreLastClear,
    clearAllHistory,
    openDrawer,
    closeDrawer,
    toggleDrawer,
    openTaskDetail,
    consumeFocusTaskId,
    taskEvents,
    fetchTaskEvents,
    startEventPolling,
    stopEventPolling,
    stopAllEventPolling,
    reset
  };
}
