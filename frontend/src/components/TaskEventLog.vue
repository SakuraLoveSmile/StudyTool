<template>
  <div class="space-y-1.5">
    <div class="flex items-center justify-between text-[11px] font-semibold text-sub">
      <span>执行日志</span>
      <button type="button" class="text-accent font-normal cursor-pointer" @click="$emit('refresh')">刷新日志</button>
    </div>
    <p v-if="syncFailed" class="text-xs text-warn" role="alert">日志读取中断，请刷新日志重试。</p>
    <div class="bg-surface rounded-lg border border-line/80 p-2 max-h-48 overflow-y-auto space-y-1.5 font-mono text-[11px]">
      <p v-if="!events?.length" class="text-faint py-3 text-center">暂无事件记录</p>
      <div v-for="{ event, details } in eventRows" :key="event.id" class="flex items-start gap-2 leading-tight">
        <span class="text-faint shrink-0">{{ formatClock(event.created_at) }}</span>
        <span class="px-1 py-0.5 rounded text-[10px] shrink-0 font-sans" :class="getEventBadgeClass(event.event_type)">{{ formatEventType(event.event_type) }}</span>
        <div class="min-w-0 flex-1 font-sans">
          <span class="text-ink break-all">{{ event.message }}</span>
          <details v-if="details.length" class="mt-1 text-sub">
            <summary class="cursor-pointer w-fit rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">诊断详情</summary>
            <dl class="mt-1 space-y-1 border-l border-line pl-2">
              <div v-for="detail in details" :key="detail.label" class="flex items-start gap-2">
                <dt class="shrink-0 text-faint">{{ detail.label }}</dt>
                <dd class="min-w-0 break-all">{{ detail.value }}</dd>
              </div>
            </dl>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { TaskEventItem } from '../types';
import { formatClock } from '../utils/time';
const props = defineProps<{ events?: TaskEventItem[]; syncFailed?: boolean }>();
defineEmits<{ refresh: [] }>();

const detailFields = [
  ['service', '服务'], ['model', '模型'], ['endpoint', '请求地址'],
  ['timeout_seconds', '单次网络等待上限（秒）'], ['attempt', '当前尝试'], ['max_attempts', '最多尝试'],
  ['call_id', '调用 ID'], ['knowledge_point_name', '知识点'], ['failed_stage', '失败阶段'], ['stage', '当前阶段'],
  ['elapsed_ms', '本次尝试耗时（毫秒）'], ['stage_elapsed_ms', '阶段耗时（毫秒）'],
  ['status_code', 'HTTP 状态'], ['provider_request_id', '上游请求 ID'],
  ['exception_type', '错误类型'], ['error_type', '错误类型'],
  ['reason_code', '校验错误编号'], ['field', '失败字段'], ['error_count', '错误数量']
] as const;

const stageLabels: Record<string, string> = {
  slot_wait: '等待服务名额',
  request_send: '准备发送请求',
  connect: '建立网络连接',
  pool_wait: '等待本地连接池',
  connect_tcp: '建立网络连接',
  tls: 'TLS 握手',
  send_request: '发送请求',
  await_headers: '等待上游响应头',
  read_body: '读取上游响应',
  http_status: '检查 HTTP 状态',
  parse_response: '解析模型结果',
  source_validation: '来源核验',
  model_call: '模型调用',
  model_request: '模型调用',
  output_validation: '卡片与引用校验',
  saving: '候选保存'
};

const diagnosticDetails = (data: TaskEventItem['data']) => {
  if (!data) return [];
  return detailFields.flatMap(([key, label]) => {
    const raw = data[key];
    if (typeof raw !== 'string' && (typeof raw !== 'number' || !Number.isFinite(raw))) return [];
    let value = String(raw).trim();
    if (!value) return [];
    if (key === 'error_type' && data.exception_type) return [];
    if (key === 'stage' || key === 'failed_stage') value = stageLabels[value] || value;
    if (key === 'endpoint') {
      try {
        const url = new URL(value);
        if (url.protocol !== 'http:' && url.protocol !== 'https:') return [];
        value = `${url.origin}${url.pathname}`;
      } catch {
        return [];
      }
    }
    return [{ label, value }];
  });
};

const eventRows = computed(() => (props.events ?? []).map(event => ({
  event,
  details: diagnosticDetails(event.data)
})));

const formatEventType = (type: string): string => {
  const map: Record<string, string> = {
    created: '创建',
    stage_change: '阶段变更',
    reading_sources: '读取资料',
    model_call: '模型生成',
    validation: '引用校验',
    validation_failed: '校验失败',
    unit_failed: '知识点失败',
    saving: '保存资料',
    slot_wait: '等待名额',
    slot_wait_started: '等待名额',
    request_start: '请求发送',
    network_stage: '网络阶段',
    response_headers: '收到响应头',
    read_body: '读取响应',
    response_body: '读取完成',
    parse_response: '解析结果',
    request_failed: '请求失败',
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
    case 'slot_wait_started':
    case 'request_start':
    case 'network_stage':
    case 'read_body':
    case 'parse_response':
      return 'bg-accent/10 text-accent';
    case 'response_headers':
    case 'response_body':
    case 'response_received':
    case 'save_done':
    case 'completed':
      return 'bg-ok/10 text-ok';
    case 'checkpoint_restored':
      return 'bg-purple-50 text-purple-700';
    case 'retry_wait':
      return 'bg-warn/10 text-warn';
    case 'failed':
    case 'request_failed':
    case 'validation_failed':
    case 'unit_failed':
      return 'bg-err/10 text-err';
    default:
      return 'bg-raised text-sub';
  }
};

</script>
