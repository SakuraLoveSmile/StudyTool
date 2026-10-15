<template>
  <div class="space-y-1.5">
    <div class="flex items-center justify-between text-[11px] font-semibold text-sub">
      <span>执行日志</span>
      <button type="button" class="text-accent font-normal cursor-pointer" @click="$emit('refresh')">刷新日志</button>
    </div>
    <p v-if="syncFailed" class="text-xs text-warn" role="alert">日志读取中断，请刷新日志重试。</p>
    <div class="bg-surface rounded-lg border border-line/80 p-2 max-h-48 overflow-y-auto space-y-1.5 font-mono text-[11px]">
      <p v-if="!events?.length" class="text-faint py-3 text-center">暂无事件记录</p>
      <div v-for="event in events" :key="event.id" class="flex items-start gap-2 leading-tight">
        <span class="text-faint shrink-0">{{ formatClock(event.created_at) }}</span>
        <span class="px-1 py-0.5 rounded text-[10px] shrink-0 font-sans" :class="getEventBadgeClass(event.event_type)">{{ formatEventType(event.event_type) }}</span>
        <span class="text-ink font-sans break-all">{{ event.message }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { TaskEventItem } from '../types';
import { formatClock } from '../utils/time';
defineProps<{ events?: TaskEventItem[]; syncFailed?: boolean }>();
defineEmits<{ refresh: [] }>();

const formatEventType = (type: string): string => {
  const map: Record<string, string> = {
    created: '创建',
    stage_change: '阶段变更',
    reading_sources: '读取资料',
    model_call: '模型生成',
    validation: '引用校验',
    saving: '保存资料',
    slot_wait: '等待名额',
    request_start: '请求发送',
    response_received: '响应返回',
    save_done: '结果入库',
    checkpoint_restored: '检查点复用',
    retry_wait: '重试等待',
    failed: '异常',
    partial: '部分完成',
    completed: '完成',
    cancelled: '取消'
  };
  return map[type] || type;
};

const getEventBadgeClass = (type: string): string => {
  switch (type) {
    case 'created':
    case 'stage_change':
      return 'bg-raised text-ink';
    case 'slot_wait':
    case 'request_start':
      return 'bg-accent/10 text-accent';
    case 'response_received':
    case 'save_done':
    case 'completed':
      return 'bg-ok/10 text-ok';
    case 'checkpoint_restored':
      return 'bg-purple-50 text-purple-700';
    case 'retry_wait':
      return 'bg-warn/10 text-warn';
    case 'failed':
      return 'bg-err/10 text-err';
    default:
      return 'bg-raised text-sub';
  }
};

</script>
