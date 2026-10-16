<template>
  <div class="page-stack" :class="{ 'android-today': isAndroidApp, 'is-focused': isAndroidApp && focusMode }">
    <header v-if="isAndroidApp && !focusMode" class="surface study-journal-header">
      <div class="journal-heading">
        <div>
          <p class="journal-eyebrow">我的学习手账 · {{ studyDate }}</p>
          <h1>今天，学一点扎实的</h1>
          <p class="journal-intro">先回想，再看答案。把读过的知识，变成自己的。</p>
        </div>
        <span class="chip" :class="statusClass(today.status)">{{ statusLabel(today.status) }}</span>
      </div>
      <div class="journal-stats" aria-label="今日学习统计">
        <div><strong>{{ today.remaining }}</strong><span>待回忆</span></div>
        <div><strong>{{ today.completed }}</strong><span>已完成 / 共 {{ today.total }}</span></div>
        <div><strong>{{ today.new_count }} / {{ today.review_count }}</strong><span>新卡 / 复习</span></div>
      </div>
      <div class="journal-progress" role="progressbar" aria-label="今日完成进度" :aria-valuenow="completionPercent" aria-valuemin="0" aria-valuemax="100">
        <span :style="{ width: `${completionPercent}%` }" />
      </div>
      <div class="journal-actions">
        <button v-if="today.status === 'paused' || today.status === 'idle'" type="button" class="btn btn-primary" :disabled="busy || loading" @click="openToday">{{ busy ? '准备中…' : today.status === 'paused' ? '继续今日学习' : '开始今日学习' }}</button>
        <button v-else-if="roundComplete && !dayComplete" type="button" class="btn btn-primary" :disabled="busy || loading" @click="advanceDay">{{ finalRound ? '完成今日学习' : '进入下一轮' }}</button>
        <button v-else-if="today.status === 'active'" type="button" class="btn btn-primary" :disabled="busy || loading" @click="pauseToday">暂停学习</button>
        <button type="button" class="btn btn-secondary" :disabled="busy || loading" @click="refreshToday()">{{ loading ? '刷新中…' : '刷新队列' }}</button>
      </div>
      <nav class="journal-shortcuts" aria-label="学习快捷入口">
        <router-link to="/library"><BookOpen aria-hidden="true" /><span>读教材<small>理解章节，梳理知识</small></span><ChevronRight aria-hidden="true" /></router-link>
        <router-link to="/recall"><Layers aria-hidden="true" /><span>回忆卡库<small>整理卡片，温故知新</small></span><ChevronRight aria-hidden="true" /></router-link>
      </nav>
    </header>
    <header v-else-if="!isAndroidApp" class="surface p-5 sm:p-6 space-y-5">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <span class="chip bg-accent/10 font-semibold text-accent">RAG V1</span>
            <span v-if="today.round" class="chip bg-raised text-sub">{{ roundLabel(today.round) }}</span>
            <span
              v-if="today.status !== 'idle'"
              class="chip"
              :class="statusClass(today.status)"
            >
              {{ statusLabel(today.status) }}
            </span>
          </div>
          <h1 class="mt-2 font-display text-xl sm:text-2xl font-bold tracking-tight text-ink">今日学习</h1>
          <p class="mt-1 max-w-2xl text-sm leading-relaxed text-sub">
            按章节范围安排主动回忆。先回想，再揭示答案，最后用难度反馈帮助系统调整下一次复习。
          </p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button type="button" class="btn btn-secondary text-xs" :disabled="loading" @click="refreshToday()">
            {{ loading ? '刷新中…' : '刷新今日队列' }}
          </button>
          <button v-if="today.status === 'paused' || today.status === 'idle'" type="button" class="btn btn-primary" :disabled="busy || loading" @click="openToday">
            {{ busy ? '准备中…' : today.status === 'paused' ? '继续今日学习' : '开始今日学习' }}
          </button>
          <button v-else-if="today.status === 'active'" type="button" class="btn btn-secondary" :disabled="busy || loading" @click="pauseToday">
            暂停学习
          </button>
          <button v-else-if="roundComplete && !dayComplete" type="button" class="btn btn-primary" :disabled="busy || loading" @click="advanceDay">
            {{ finalRound ? '完成今日学习' : '进入下一轮' }}
          </button>
        </div>
      </div>

      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-xl border border-line/70 bg-raised/80 px-4 py-3.5">
          <span class="text-xs font-medium text-sub">今日总量</span>
          <strong class="mt-1 block text-2xl font-bold tracking-tight text-ink">{{ today.total }}</strong>
        </div>
        <div class="rounded-xl border border-ok/30/60 bg-ok/10 px-4 py-3.5">
          <span class="text-xs font-medium text-ok/80">已完成</span>
          <strong class="mt-1 block text-2xl font-bold tracking-tight text-ok">{{ today.completed }}</strong>
        </div>
        <div class="rounded-xl border border-accent/30 bg-accent/10 px-4 py-3.5">
          <span class="text-xs font-medium text-accent/80">待复习</span>
          <strong class="mt-1 block text-2xl font-bold tracking-tight text-accent">{{ today.remaining }}</strong>
        </div>
        <div class="rounded-xl border border-warn/30/60 bg-warn/10 px-4 py-3.5">
          <span class="text-xs font-medium text-warn/80">新卡 / 复习</span>
          <strong class="mt-1 block text-2xl font-bold tracking-tight text-warn">{{ today.new_count }} / {{ today.review_count }}</strong>
        </div>
      </div>

      <div class="h-2 overflow-hidden rounded-full bg-raised" aria-label="今日学习进度">
        <div class="h-full rounded-full bg-gradient-to-r from-brand-400 to-brand-600 transition-all" :style="{ width: `${completionPercent}%` }" />
      </div>
    </header>

    <div v-if="pageError" class="rounded-xl border border-err/30 bg-err/10 px-4 py-3 text-sm text-err" role="alert">
      {{ pageError }}
    </div>

    <div v-if="isAndroidApp" class="study-focus-toolbar">
      <p>{{ focusMode ? '专注背诵' : '今日回忆' }}<span v-if="today.round"> · {{ roundLabel(today.round) }}</span></p>
      <button v-if="today.current_attempt || focusMode" type="button" class="btn btn-secondary" :aria-pressed="focusMode" @click="focusMode = !focusMode">{{ focusMode ? '退出专注' : '专注背诵' }}</button>
    </div>
    <div class="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]" :class="{ 'study-layout': isAndroidApp }">
      <div>
        <RecallSession
          :attempt="today.current_attempt"
          :busy="busy"
          :source-excerpt="today.current_card?.source_excerpt"
          :show-hints="settings.show_hints !== false"
          :round-complete="roundComplete"
          :day-complete="dayComplete"
          :final-round="finalRound"
          :focused="isAndroidApp && focusMode"
          :session-state="today.status"
          @reveal="revealCurrent"
          @rate="rateCurrent"
          @next="nextCard"
          @skip="skipCard"
          @pause="pauseToday"
          @advance="advanceDay"
        />
      </div>

      <aside v-if="!isAndroidApp || !focusMode" class="space-y-4 study-aside">
        <button v-if="isAndroidApp" type="button" class="btn btn-secondary study-options-toggle" :aria-expanded="studyOptionsOpen" aria-controls="today-study-options" @click="studyOptionsOpen = !studyOptionsOpen">
          学习范围与设置 <ChevronDown aria-hidden="true" :class="{ 'rotate-180': studyOptionsOpen }" />
        </button>
        <div v-if="isAndroidApp && !studyOptionsOpen" class="study-collapsed-feedback" aria-live="polite">
          <p v-if="scopeError || settingsError" class="text-err" role="alert">{{ scopeError || settingsError }} 请展开设置查看。</p>
          <p v-else-if="scopeNotice || settingsNotice" class="text-ok">{{ scopeNotice || settingsNotice }}</p>
        </div>
        <div id="today-study-options" v-show="!isAndroidApp || studyOptionsOpen" class="space-y-4 study-options-content">
        <section class="surface p-4 sm:p-5">
          <div class="flex items-center justify-between gap-2">
            <div>
              <h2 class="text-sm font-semibold text-ink">学习范围</h2>
              <p class="mt-1 text-xs text-sub">选择今天参与队列的章节</p>
            </div>
            <button type="button" class="text-xs font-medium text-accent hover:underline disabled:opacity-50" :disabled="savingScope" @click="saveScope">
              {{ savingScope ? '保存中…' : '保存' }}
            </button>
          </div>
          <div class="mt-3 min-h-10 text-xs leading-4">
            <p v-if="scopeNotice" class="rounded-lg border border-ok/30 bg-ok/10 px-3 py-2 text-ok" role="status" aria-live="polite">{{ scopeNotice }}</p>
            <div v-if="scopeError" class="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-err" role="alert">
              {{ scopeError }}
              <button v-if="scopeRefreshFailed" type="button" class="mt-2 block font-medium underline" :disabled="loading" @click="retryScopeRefresh">重试刷新</button>
            </div>
          </div>
          <div v-if="chapters.length === 0" class="mt-4 rounded-lg bg-raised px-3 py-3 text-xs text-faint">
            暂无章节，请先在回忆卡页面创建章节或导入内容。
          </div>
          <div v-else class="mt-4 max-h-64 space-y-1.5 overflow-y-auto pr-1">
            <label
              v-for="chapter in chapters"
              :key="chapter.id"
              class="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-xs transition-colors hover:bg-raised"
            >
              <input v-model="selectedChapterIds" @change="markScopeEdited" type="checkbox" :value="chapter.id" :disabled="savingScope" class="rounded border-line-strong text-accent focus:ring-accent" />
              <span class="min-w-0 flex-1 truncate text-ink">{{ chapter.name }}</span>
              <span class="shrink-0 text-[11px] text-faint">{{ chapter.card_count }}</span>
            </label>
          </div>
          <label class="mt-3 flex cursor-pointer items-center gap-2 border-t border-line pt-3 text-xs text-sub">
            <input v-model="includeUnassigned" @change="markScopeEdited" type="checkbox" :disabled="savingScope" class="rounded border-line-strong text-accent focus:ring-accent" />
            包含未分类卡片
          </label>
        </section>

        <section class="surface p-4 sm:p-5">
          <div class="flex items-center justify-between gap-2">
            <div>
              <h2 class="text-sm font-semibold text-ink">今日设置</h2>
              <p class="mt-1 text-xs text-sub">限制每次打开的学习量</p>
            </div>
            <button type="button" class="text-xs font-medium text-accent hover:underline disabled:opacity-50" :disabled="savingSettings" @click="saveSettings">
              {{ savingSettings ? '保存中…' : '保存' }}
            </button>
          </div>
          <div class="mt-3 min-h-10 text-xs leading-4">
            <p v-if="settingsNotice" class="rounded-lg border border-ok/30 bg-ok/10 px-3 py-2 text-ok" role="status" aria-live="polite">{{ settingsNotice }}</p>
            <div v-if="settingsError" class="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-err" role="alert">
              {{ settingsError }}
              <button v-if="settingsRefreshFailed" type="button" class="mt-2 block font-medium underline" :disabled="loading" @click="retrySettingsRefresh">重试刷新</button>
            </div>
          </div>
          <div class="mt-4 space-y-3 text-xs">
            <label class="block">
              <span class="field-label">每日新卡上限（1–100）</span>
              <input v-model.number="settings.daily_new_limit" @input="markSettingsEdited" type="number" min="1" max="100" step="1" class="field" :disabled="savingSettings" placeholder="未配置" />
            </label>
            <label class="block">
              <span class="field-label">每日复习上限（1–100）</span>
              <input v-model.number="settings.daily_review_limit" @input="markSettingsEdited" type="number" min="1" max="100" class="field" :disabled="savingSettings" placeholder="未配置" />
            </label>
            <label class="flex cursor-pointer items-center gap-2 text-sub">
              <input v-model="settings.auto_advance" @change="markSettingsEdited" type="checkbox" :disabled="savingSettings" class="rounded border-line-strong text-accent focus:ring-accent" />
              评分后自动进入下一张
            </label>
            <label class="flex cursor-pointer items-center gap-2 text-sub">
              <input v-model="settings.show_hints" @change="markSettingsEdited" type="checkbox" :disabled="savingSettings" class="rounded border-line-strong text-accent focus:ring-accent" />
              揭示前显示提示
            </label>
          </div>
        </section>

        <section v-if="today.queue.length > 0" class="surface p-4 sm:p-5">
          <h2 class="text-sm font-semibold text-ink">队列预览</h2>
          <p class="mt-1 text-xs text-sub">到期复习与续接优先；未完成前不会加入新学内容。</p>
          <ul class="mt-3 space-y-1.5">
            <li
              v-for="(card, index) in today.queue.slice(0, 8)"
              :key="`${card.id}-${index}`"
              class="flex items-start gap-2 rounded-lg border border-line px-3 py-2 text-xs"
            >
              <span class="shrink-0 text-faint">{{ index + 1 }}.</span>
              <span class="min-w-0 flex-1 text-ink">
                <span class="line-clamp-2">{{ card.prompt }}</span>
                <span v-if="card.due_at" class="mt-0.5 block text-[11px] text-faint">到期 {{ card.due_at }}</span>
              </span>
            </li>
          </ul>
          <label class="mt-3 flex cursor-pointer items-start gap-2 border-t border-line pt-3 text-xs text-sub">
            <input
              type="checkbox"
              class="mt-0.5 rounded border-line-strong text-accent focus:ring-accent"
              :checked="today.override_enabled"
              :disabled="busy || today.status !== 'active'"
              @change="toggleOverride(($event.target as HTMLInputElement).checked)"
            />
            <span>
              今日临时覆盖：允许在到期复习未完成时也加入新学内容
              <span class="mt-0.5 block text-[11px] text-warn">仅影响今天，次日自动恢复默认优先级。</span>
            </span>
          </label>
        </section>
        </div>
      </aside>
      </div>
    </div>

    <div v-if="emptyReason === 'no_chapters'" class="surface flex flex-col items-center p-8 text-center">
      <h2 class="font-display text-lg font-semibold text-ink">先创建一个章节</h2>
      <p class="mt-1 text-sm text-sub">有章节后才能建立回忆卡并开始学习。</p>
      <router-link to="/recall" class="btn btn-primary mt-4">去创建章节</router-link>
    </div>
    <div v-else-if="emptyReason === 'no_published_cards'" class="surface flex flex-col items-center p-8 text-center">
      <h2 class="font-display text-lg font-semibold text-ink">还没有可学习的卡片</h2>
      <p class="mt-1 text-sm text-sub">先创建并审核发布一张回忆卡。</p>
      <router-link to="/recall" class="btn btn-primary mt-4">去管理回忆卡</router-link>
    </div>

    <!-- Day Changed Cross-day Modal -->
    <BaseModal
      :show="dayChangedModal"
      title="学习日已跨天切换"
      max-width="md"
      @close="handleDayChangedClose"
    >
      <div class="space-y-3 text-sm text-sub">
        <div class="rounded-xl border border-warn/30 bg-warn/10 p-3.5 text-xs text-warn">
          {{ dayChangedMessage || '检测到系统日期已发生变化（例如跨过午夜），今日任务队列已自动切换。' }}
        </div>
        <p class="text-xs text-faint">
          点击下方按钮将重新加载今日学习队列与调度状态，继续新一天的学习。
        </p>
      </div>
      <template #footer>
        <button type="button" class="btn btn-primary w-full" :disabled="loading" @click="handleDayChangedConfirm">
          {{ loading ? '刷新中…' : '立即刷新今日学习' }}
        </button>
      </template>
    </BaseModal>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { BookOpen, ChevronDown, ChevronRight, Layers } from 'lucide-vue-next';
import { isAndroidApp } from '../utils/clientPlatform';
import { apiClient } from '../api/client';
import BaseModal from '../components/BaseModal.vue';
import RecallSession from '../components/RecallSession.vue';
import type { AttemptRating, LearningChapter, LearningScope, LearningSettings, TodayLearning } from '../types/learning';
import { manualRatedAttempt, normaliseScope, normaliseSettings, normaliseToday } from '../types/learning';

const focusMode = ref(false);
const studyOptionsOpen = ref(false);
const studyDate = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date());

const newRequestId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
};

const defaultSettings: LearningSettings = {
  daily_new_limit: null,
  daily_review_limit: null,
  reveal_after_seconds: null,
  auto_advance: false,
  show_hints: true,
  timezone: 'Asia/Shanghai',
};

const today = ref<TodayLearning>(normaliseToday(null));
watch(() => today.value.status, status => {
  if (status === 'idle' || status === 'paused') focusMode.value = false;
});
const settings = ref<LearningSettings>({ ...defaultSettings });
const chapters = ref<LearningChapter[]>([]);
const selectedChapterIds = ref<string[]>([]);
const includeUnassigned = ref(true);
const loading = ref(false);
const busy = ref(false);
const savingSettings = ref(false);
const savingScope = ref(false);
const pageError = ref('');
const settingsNotice = ref('');
const settingsError = ref('');
const scopeNotice = ref('');
const scopeError = ref('');
const settingsEditVersion = ref(0);
const scopeEditVersion = ref(0);
const settingsDirty = ref(false);
const scopeDirty = ref(false);
const settingsRefreshFailed = ref(false);
const scopeRefreshFailed = ref(false);
const todayRefreshVersion = ref(0);
const ratingRequestIds = ref<Record<string, string>>({});
const dayChangedModal = ref(false);
const dayChangedMessage = ref('');

const checkDayChangedError = (error: unknown): boolean => {
  const err = error as any;
  const detail = err?.response?.data?.detail;
  const code = detail?.code || (typeof detail === 'object' ? detail?.error : null);
  const msg = typeof detail === 'string' ? detail : detail?.message || '';
  if (code === 'DAY_CHANGED' || (typeof msg === 'string' && msg.includes('学习日已切换'))) {
    dayChangedMessage.value = typeof msg === 'string' && msg ? msg : '学习日已切换，请刷新今日学习。';
    dayChangedModal.value = true;
    return true;
  }
  return false;
};

const handleDayChangedConfirm = async () => {
  dayChangedModal.value = false;
  pageError.value = '';
  await loadPage();
};

const handleDayChangedClose = () => {
  dayChangedModal.value = false;
};

const todayRecord = computed(() => today.value as TodayLearning & Record<string, unknown>);
const emptyReason = computed(() => typeof todayRecord.value.empty_reason === 'string' ? todayRecord.value.empty_reason : null);
const roundComplete = computed(() => todayRecord.value.round_complete === true || emptyReason.value === 'round_completed');
const finalRound = computed(() => today.value.round === 3 || today.value.round === 'evening');
const dayComplete = computed(() => todayRecord.value.day_completed === true || emptyReason.value === 'day_completed' || today.value.status === 'completed');

const completionPercent = computed(() => {
  if (!today.value.total) return 0;
  return Math.min(100, Math.round((today.value.completed / today.value.total) * 100));
});

const statusLabel = (status: TodayLearning['status']): string => {
  const labels: Record<TodayLearning['status'], string> = {
    idle: '未开始',
    active: '学习中',
    paused: '已暂停',
    completed: '已完成',
  };
  return labels[status];
};

const statusClass = (status: TodayLearning['status']): string => {
  if (status === 'active') return 'bg-accent/10 text-accent';
  if (status === 'completed') return 'bg-ok/10 text-ok';
  if (status === 'paused') return 'bg-warn/10 text-warn';
  return 'bg-raised text-sub';
};

const roundLabel = (round: NonNullable<TodayLearning['round']>): string => {
  if (typeof round === 'number') return `第 ${round} 轮`;
  const labels: Record<string, string> = {
    morning: '早晨轮次',
    noon: '中午轮次',
    evening: '晚间轮次',
  };
  return labels[round] ?? round;
};

const applyToday = (value: TodayLearning, options: { preserveSettings?: boolean; preserveScope?: boolean } = {}) => {
  today.value = value;
  if (!options.preserveSettings && value.settings) settings.value = { ...settings.value, ...value.settings };
  if (!options.preserveScope && value.scope) applyScope(value.scope);
};

const applyScope = (scope: LearningScope) => {
  selectedChapterIds.value = [...scope.chapter_ids];
  includeUnassigned.value = scope.include_unassigned;
};

const refreshToday = async (options: { silentPageError?: boolean } = {}): Promise<boolean> => {
  const refreshVersion = ++todayRefreshVersion.value;
  const settingsVersionAtRequest = settingsEditVersion.value;
  const scopeVersionAtRequest = scopeEditVersion.value;
  loading.value = true;
  if (!options.silentPageError) pageError.value = '';
  try {
    const result = await apiClient.getTodayLearning();
    if (refreshVersion !== todayRefreshVersion.value) return true;
    applyToday(result, {
      preserveSettings: settingsDirty.value || savingSettings.value || settingsVersionAtRequest !== settingsEditVersion.value,
      preserveScope: scopeDirty.value || savingScope.value || scopeVersionAtRequest !== scopeEditVersion.value,
    });
    return true;
  } catch (error) {
    if (refreshVersion !== todayRefreshVersion.value) return true;
    if (!options.silentPageError) pageError.value = error instanceof Error ? error.message : '加载今日学习失败，请稍后重试。';
    return false;
  } finally {
    if (refreshVersion === todayRefreshVersion.value) loading.value = false;
  }
};

const markSettingsEdited = () => {
  settingsEditVersion.value += 1;
  settingsDirty.value = true;
  settingsNotice.value = '';
  settingsError.value = '';
  settingsRefreshFailed.value = false;
};

const markScopeEdited = () => {
  scopeEditVersion.value += 1;
  scopeDirty.value = true;
  scopeNotice.value = '';
  scopeError.value = '';
  scopeRefreshFailed.value = false;
};

const openToday = async () => {
  busy.value = true;
  pageError.value = '';
  try {
    applyToday(await apiClient.openTodayLearning({
      day_version: today.value.state_version || undefined,
      resume: today.value.status === 'paused',
    }));
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '打开今日学习失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const pauseToday = async () => {
  busy.value = true;
  try {
    applyToday(await apiClient.pauseTodayLearning({ day_version: today.value.state_version || undefined }));
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '暂停学习失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const revealCurrent = async () => {
  const attempt = today.value.current_attempt;
  if (!attempt) return;
  busy.value = true;
  try {
    const action = await apiClient.revealLearningAttempt(attempt.id, {
      day_unit_id: attempt.day_unit_id || undefined,
      day_version: today.value.state_version || undefined,
    });
    const revealed = action.unit ?? action.attempt;
    if (revealed) {
      today.value.current_attempt = revealed;
      today.value.current_card = revealed.card ?? today.value.current_card;
    } else {
      await refreshToday();
    }
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '揭示答案失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const rateCurrent = async (rating: AttemptRating) => {
  const previousDay = today.value;
  const attempt = previousDay.current_attempt;
  if (!attempt) return;
  busy.value = true;
  try {
    const requestId = ratingRequestIds.value[attempt.id] ?? newRequestId();
    ratingRequestIds.value[attempt.id] = requestId;
    const action = await apiClient.rateLearningAttempt(attempt.id, {
      rating,
      request_id: requestId,
      content_revision_no: attempt.content_revision_no || undefined,
      day_unit_id: attempt.day_unit_id || undefined,
      day_version: today.value.state_version || undefined,
    });
    const refreshed = await refreshToday();
    if (!refreshed) {
      // The write succeeded, but the day may have changed; do not offer another
      // rating or restore a card against an unconfirmed day version.
      if (today.value.current_attempt?.id === attempt.id) {
        today.value.current_attempt = null;
        today.value.current_card = null;
      }
      pageError.value = `评分已记录，但今日队列刷新失败，请刷新队列后继续。${pageError.value}`;
      return;
    }
    if (!settings.value.auto_advance) {
      const retained = manualRatedAttempt(today.value, action, previousDay);
      if (retained) {
        today.value.current_attempt = retained;
        today.value.current_card = retained.card ?? today.value.queue.find(card => card.id === retained.card_id) ?? null;
      }
    } else if (!roundComplete.value && today.value.current_attempt?.id === attempt.id) {
      await nextCard();
    }
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '记录难度失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const advanceDay = async () => {
  busy.value = true;
  try {
    applyToday(await apiClient.advanceTodayLearning({ day_version: today.value.state_version || undefined }));
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '推进学习轮次失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const nextCard = async () => {
  busy.value = true;
  try {
    if (today.value.current_attempt && !today.value.current_attempt.completed && today.value.current_attempt.status !== 'rated') return;
    applyToday(await apiClient.nextTodayLearning({
      day_unit_id: today.value.current_attempt?.day_unit_id || undefined,
      day_version: today.value.state_version || undefined,
    }));
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '进入下一张失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const skipCard = async () => {
  await nextCard();
};

const toggleOverride = async (enabled: boolean) => {
  busy.value = true;
  try {
    applyToday(await apiClient.overrideTodayLearning({
      enabled,
      day_version: today.value.state_version || undefined,
    }));
  } catch (error) {
    if (checkDayChangedError(error)) return;
    pageError.value = error instanceof Error ? error.message : '切换今日覆盖失败，请稍后重试。';
  } finally {
    busy.value = false;
  }
};

const clampDailyLimit = (value: number | null | undefined): number | null => {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return Math.min(100, Math.max(1, Math.round(parsed)));
};

const nullableDailyLimit = (value: number | null | undefined): number | null => {
  if (value === null || value === undefined) return null;
  return clampDailyLimit(value);
};

const saveSettings = async () => {
  if (savingSettings.value) return;
  todayRefreshVersion.value += 1;
  loading.value = false;
  const editVersion = settingsEditVersion.value;
  const payload = {
    daily_new_limit: nullableDailyLimit(settings.value.daily_new_limit),
    daily_review_limit: nullableDailyLimit(settings.value.daily_review_limit),
    reveal_after_seconds: settings.value.reveal_after_seconds === null || settings.value.reveal_after_seconds === undefined ? null : Math.max(0, Number(settings.value.reveal_after_seconds) || 0),
    auto_advance: settings.value.auto_advance,
    show_hints: settings.value.show_hints,
  };
  savingSettings.value = true;
  settingsNotice.value = '';
  settingsError.value = '';
  settingsRefreshFailed.value = false;
  try {
    const saved = await apiClient.updateLearningSettings(payload);
    if (editVersion === settingsEditVersion.value) {
      settings.value = saved;
      settingsDirty.value = false;
    }
    const refreshed = await refreshToday({ silentPageError: true });
    if (!refreshed) {
      settingsError.value = '今日设置已保存，但今日队列刷新失败，请重试刷新。';
      settingsRefreshFailed.value = true;
    } else if (editVersion === settingsEditVersion.value) {
      settingsNotice.value = '今日设置已保存';
    }
  } catch (error) {
    settingsError.value = error instanceof Error ? error.message : '保存设置失败，请稍后重试。';
  } finally {
    savingSettings.value = false;
  }
};

const saveScope = async () => {
  if (savingScope.value) return;
  todayRefreshVersion.value += 1;
  loading.value = false;
  const editVersion = scopeEditVersion.value;
  const payload = {
    chapter_ids: [...selectedChapterIds.value],
    include_unassigned: includeUnassigned.value,
  };
  savingScope.value = true;
  scopeNotice.value = '';
  scopeError.value = '';
  scopeRefreshFailed.value = false;
  try {
    const scope = await apiClient.updateLearningScope(payload);
    if (editVersion === scopeEditVersion.value) {
      applyScope(normaliseScope(scope));
      scopeDirty.value = false;
    }
    const refreshed = await refreshToday({ silentPageError: true });
    if (!refreshed) {
      scopeError.value = '学习范围已保存，但今日队列刷新失败，请重试刷新。';
      scopeRefreshFailed.value = true;
    } else {
      if (today.value.status === 'idle' && (today.value.total > 0 || today.value.remaining > 0)) await openToday();
      if (editVersion === scopeEditVersion.value) scopeNotice.value = '学习范围已保存';
    }
  } catch (error) {
    scopeError.value = error instanceof Error ? error.message : '保存学习范围失败，请稍后重试。';
  } finally {
    savingScope.value = false;
  }
};

const retrySettingsRefresh = async () => {
  if (await refreshToday({ silentPageError: true })) {
    settingsRefreshFailed.value = false;
    settingsError.value = '';
    settingsNotice.value = '今日设置已保存';
  }
};

const retryScopeRefresh = async () => {
  if (await refreshToday({ silentPageError: true })) {
    scopeRefreshFailed.value = false;
    scopeError.value = '';
    scopeNotice.value = '学习范围已保存';
  }
};

const loadPage = async () => {
  const settingsVersionAtRequest = settingsEditVersion.value;
  const scopeVersionAtRequest = scopeEditVersion.value;
  loading.value = true;
  pageError.value = '';
  try {
    const [todayResult, settingsResult, chapterResult] = await Promise.all([
      apiClient.getTodayLearning(),
      apiClient.getLearningSettings(),
      apiClient.listLearningChapters(),
    ]);
    applyToday(todayResult, {
      preserveSettings: settingsDirty.value || settingsVersionAtRequest !== settingsEditVersion.value,
      preserveScope: scopeDirty.value || scopeVersionAtRequest !== scopeEditVersion.value,
    });
    if (!settingsDirty.value && settingsVersionAtRequest === settingsEditVersion.value) {
      settings.value = { ...defaultSettings, ...normaliseSettings(settingsResult) };
    }
    chapters.value = chapterResult;
    if (!scopeDirty.value && scopeVersionAtRequest === scopeEditVersion.value) {
      if (todayResult.scope) applyScope(normaliseScope(todayResult.scope));
      else applyScope(normaliseScope(settingsResult));
    }
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载学习数据失败，请稍后重试。';
  } finally {
    loading.value = false;
  }
};

onMounted(loadPage);
</script>

<style scoped>
.android-today { gap: 20px; }
.study-journal-header { padding: 24px 20px 20px; }
.journal-heading { display: flex; align-items: flex-start; gap: 12px; justify-content: space-between; }
.journal-heading > div { min-width: 0; }
.journal-eyebrow { color: rgb(var(--c-accent)); font-size: 12px; font-weight: 600; letter-spacing: .06em; }
.journal-heading h1 { margin-top: 10px; color: rgb(var(--c-ink)); font-size: 24px; font-weight: 750; line-height: 1.45; letter-spacing: -.025em; }
.journal-intro { margin-top: 8px; font-size: 13px; line-height: 1.8; color: rgb(var(--c-sub)); }
.journal-heading > .chip { flex-shrink: 0; }
.journal-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 28px; }
.journal-stats > div { display: flex; flex-direction: column; gap: 4px; border-right: 1px solid rgb(var(--c-line)); padding-left: 12px; }
.journal-stats > div:first-child { padding-left: 0; }
.journal-stats > div:last-child { border: 0; }
.journal-stats strong { color: rgb(var(--c-accent)); font-size: 24px; font-weight: 700; line-height: 1.4; }
.journal-stats span { font-size: 11px; color: rgb(var(--c-sub)); }
.journal-progress { height: 5px; margin-top: 20px; overflow: hidden; border-radius: 99px; background: rgb(var(--c-raised)); }
.journal-progress span { display: block; height: 100%; border-radius: inherit; background: rgb(var(--c-accent)); transition: width .2s; }
.journal-actions { display: flex; gap: 10px; margin-top: 20px; }
.journal-actions > .btn:first-child { flex: 1; }
.android-today :deep(.btn) { min-height: 48px; }
.journal-shortcuts { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 20px; border-top: 1px solid rgb(var(--c-line)); padding-top: 18px; }
.journal-shortcuts a { display: flex; align-items: center; gap: 9px; min-height: 48px; color: rgb(var(--c-ink)); font-size: 13px; font-weight: 650; }
.journal-shortcuts svg { width: 20px; height: 20px; flex-shrink: 0; color: rgb(var(--c-accent)); }
.journal-shortcuts a > svg:last-child { width: 14px; height: 14px; margin-left: auto; }
.journal-shortcuts small { display: block; margin-top: 3px; font-size: 10px; font-weight: 400; color: rgb(var(--c-sub)); line-height: 1.6; }
.study-focus-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.study-focus-toolbar p { color: rgb(var(--c-ink)); font-size: 14px; font-weight: 650; }
.study-focus-toolbar p span { color: rgb(var(--c-sub)); font-size: 12px; font-weight: 400; }
.study-options-toggle { width: 100%; justify-content: space-between; }
.study-options-toggle svg { width: 18px; height: 18px; }
.study-collapsed-feedback { font-size: 12px; line-height: 1.8; }
.android-today .study-aside :deep(label) { min-height: 44px; }
.android-today .study-aside :deep(input[type='checkbox']) { width: 18px; height: 18px; flex-shrink: 0; }
.android-today .study-aside :deep(button) { min-height: 44px; }
.android-today .study-aside :deep(.field) { min-height: 48px; font-size: 16px; }
.android-today.is-focused .study-layout { grid-template-columns: minmax(0, 1fr); }
@media (min-width: 840px) {
  .study-journal-header { padding: 28px; }
  .journal-heading h1 { font-size: 28px; }
  .journal-stats { max-width: 650px; }
  .journal-actions { max-width: 440px; }
  .journal-shortcuts { max-width: 650px; }
  .journal-shortcuts small { font-size: 12px; }
  .study-layout { grid-template-columns: minmax(0, 1fr) 250px; align-items: start; gap: 20px; }
  .study-options-toggle, .study-collapsed-feedback { display: none; }
  .study-options-content { display: block !important; }
  .android-today.is-focused { max-width: 1100px; margin-inline: auto; width: 100%; }
}
</style>
