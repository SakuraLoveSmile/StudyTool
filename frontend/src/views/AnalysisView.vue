<template>
  <div class="page-stack">
    <!-- Top Overall Statistics Banner -->
    <div class="surface p-5 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-slate-100 pb-4">
        <div>
          <h2 class="text-lg font-bold text-slate-900 tracking-tight">历年考情看板与考点目录</h2>
          <p class="text-xs text-slate-500 mt-1">
            严守真实性原则：仅已确认真题参与频次统计。支持从上传教材智能梳理考点，低频与未考考点完整保留。
          </p>
        </div>

        <!-- Toolbar Actions -->
        <div class="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            class="btn btn-primary text-xs cursor-pointer"
            @click="isExtractionModalOpen = true"
          >
            <Sparkles class="w-3.5 h-3.5 mr-1 text-amber-300" />
            从资料提取考点
          </button>

          <button
            type="button"
            class="btn btn-secondary text-xs cursor-pointer"
            :disabled="isRefreshing"
            @click="handleRefresh"
          >
            <RotateCw class="w-3.5 h-3.5 mr-1" :class="{ 'animate-spin': isRefreshing }" />
            <span>{{ isRefreshing ? '刷新中...' : '刷新数据' }}</span>
          </button>

          <!-- More Options Dropdown -->
          <div class="relative">
            <button
              type="button"
              class="btn btn-secondary text-xs"
              @click="showMoreMenu = !showMoreMenu"
            >
              更多操作 ▾
            </button>
            <div
              v-if="showMoreMenu"
              class="absolute right-0 mt-1 w-44 bg-white rounded-xl shadow-lg border border-slate-200 py-1.5 z-20 text-xs text-slate-700"
              @click="showMoreMenu = false"
            >
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-slate-50 flex items-center gap-2 cursor-pointer"
                @click="openAddKpModal"
              >
                <Plus class="w-3.5 h-3.5 text-slate-400" />
                手动新增考点
              </button>
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-slate-50 flex items-center gap-2 cursor-pointer"
                @click="openMergeModal"
              >
                <Merge class="w-3.5 h-3.5 text-slate-400" />
                合并同义考点
              </button>
              <div class="h-px bg-slate-100 my-1" />
              <button
                type="button"
                class="w-full text-left px-3.5 py-2 hover:bg-slate-50 flex items-center gap-2 text-amber-700 cursor-pointer"
                @click="openLegacyModal"
              >
                <HelpCircle class="w-3.5 h-3.5 text-amber-500" />
                管理旧考点 ({{ legacyCount }})
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- KPI Summary Cards -->
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3.5">
        <div class="bg-slate-50/70 p-3.5 rounded-xl border border-slate-100">
          <span class="text-xs text-slate-500">已收录真题年份</span>
          <div class="text-xl font-bold text-slate-800 mt-1">
            {{ overallStats.total_recorded_years }} <span class="text-xs font-normal text-slate-500">个年份</span>
          </div>
          <span class="text-xs text-slate-400 mt-0.5 block">
            跨度: {{ overallStats.min_year || '-' }} ~ {{ overallStats.max_year || '-' }}
          </span>
        </div>

        <div class="bg-slate-50/70 p-3.5 rounded-xl border border-slate-100">
          <span class="text-xs text-slate-500">已确认真题 / 题目总量</span>
          <div class="text-xl font-bold mt-1" :class="overallStats.confirmed_questions > 0 ? 'text-blue-600' : 'text-slate-700'">
            {{ overallStats.confirmed_questions }} <span class="text-xs font-normal text-slate-500">/ {{ overallStats.total_questions }}</span>
          </div>
          <span class="text-xs text-slate-500 mt-0.5 block">
            {{ overallStats.confirmed_questions > 0 ? '已确认题目计入考情统计' : '暂无确认真题，未计考频' }}
          </span>
        </div>

        <div class="bg-slate-50/70 p-3.5 rounded-xl border border-slate-100">
          <span class="text-xs text-slate-500">考点目录总数</span>
          <div class="text-xl font-bold text-slate-800 mt-1">
            {{ overallStats.knowledge_points_count }} <span class="text-xs font-normal text-slate-500">个考点</span>
          </div>
          <span class="text-xs text-slate-400 mt-0.5 block">
            包含已提取与录入考点
          </span>
        </div>

        <div class="bg-slate-50/70 p-3.5 rounded-xl border border-slate-100">
          <span class="text-xs text-slate-500">近五年统计基准窗</span>
          <div class="text-xl font-bold text-amber-600 mt-1">
            {{ overallStats.recent_5yr_window.length }} <span class="text-xs font-normal text-slate-500">/ 5 年已收录</span>
          </div>
          <span class="text-xs text-slate-400 mt-0.5 block">
            以最新已确认真题为准
          </span>
        </div>
      </div>

      <!-- Year Breakdown -->
      <div class="text-xs bg-slate-50/80 p-3 rounded-xl border border-slate-100 space-y-1.5">
        <div class="flex items-center gap-2 text-slate-600">
          <Calendar class="w-3.5 h-3.5 text-blue-600" />
          <span class="font-medium">年份收录完整性：</span>
          <span v-if="lastRefreshTime" class="text-slate-400 text-[11px] ml-auto">
            上次更新: {{ lastRefreshTime }}
          </span>
        </div>
        <div class="flex flex-wrap gap-1.5 items-center">
          <span
            v-for="y in overallStats.recorded_years"
            :key="y"
            class="px-2 py-0.5 rounded text-xs bg-emerald-100 text-emerald-800 font-medium"
          >
            ✓ {{ y }}
          </span>
          <span
            v-for="my in overallStats.missing_years"
            :key="my"
            class="px-2 py-0.5 rounded text-xs bg-rose-100 text-rose-700 font-medium"
          >
            ✗ 缺失 {{ my }}
          </span>
          <span v-if="overallStats.recorded_years.length === 0" class="text-slate-400">
            暂无已确认真题年份。导入真题并确认后将自动计算年份覆盖。
          </span>
        </div>
      </div>
    </div>

    <!-- Running / actionable task summary (含资料解析任务；点击直达任务详情) -->
    <RunningTasksPanel />

    <!-- Filter & Search Toolbar -->
    <div class="page-toolbar">
      <div class="flex items-center gap-3 flex-wrap">
        <!-- Search Input -->
        <div class="relative min-w-[200px]">
          <input
            v-model="searchKeyword"
            type="text"
            placeholder="搜索考点名称或章节..."
            class="field py-1.5 text-xs sm:text-sm pl-8 min-h-[36px]"
          />
          <Search class="w-4 h-4 text-slate-400 absolute left-2.5 top-2.5" />
        </div>

        <!-- Chapter Filter -->
        <select
          v-model="selectedChapter"
          class="field py-1.5 text-xs sm:text-sm min-h-[36px] w-auto"
        >
          <option value="">全部章节</option>
          <option v-for="ch in chaptersList" :key="ch" :value="ch">
            {{ ch }}
          </option>
        </select>
      </div>

      <!-- Priority Filter Tabs -->
      <div class="flex items-center gap-1 bg-slate-100 p-1 rounded-lg text-xs font-medium">
        <button
          v-for="tab in priorityTabs"
          :key="tab.value"
          type="button"
          class="px-2.5 py-1.5 rounded-md transition-all cursor-pointer"
          :class="selectedPriority === tab.value ? 'bg-white text-blue-700 shadow-xs font-semibold' : 'text-slate-600 hover:text-slate-900'"
          @click="selectedPriority = tab.value"
        >
          {{ tab.label }}
        </button>
      </div>
    </div>

    <!-- Knowledge Point Stats Table -->
    <div class="surface overflow-hidden">
      <div v-if="filteredPointStats.length === 0">
        <EmptyState
          title="未找到匹配的考点"
          description="尝试更换筛选条件，或者点击上方「从资料提取考点」自动从教材梳理考点。"
        >
          <template #action>
            <button
              type="button"
              class="btn btn-sm btn-primary"
              @click="isExtractionModalOpen = true"
            >
              从资料提取考点
            </button>
          </template>
        </EmptyState>
      </div>

      <div v-else class="overflow-x-auto">
        <table class="w-full text-left border-collapse text-xs sm:text-sm">
          <thead>
            <tr class="bg-slate-50 border-b border-slate-200 text-slate-600 font-semibold uppercase tracking-wider text-xs">
              <th class="py-3 px-4">知识点与出处</th>
              <th class="py-3 px-3 text-center">优先级</th>
              <th class="py-3 px-3 text-center">累计考题</th>
              <th class="py-3 px-3 text-center">考查年份数</th>
              <th class="py-3 px-3 text-center">近五年频次</th>
              <th class="py-3 px-3 text-center">最近考查</th>
              <th class="py-3 px-4 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr
              v-for="item in filteredPointStats"
              :key="item.id"
              class="hover:bg-slate-50/70 transition-colors"
            >
              <!-- Name & Chapter & Source Badge -->
              <td class="py-3.5 px-4">
                <div class="flex items-center gap-2 flex-wrap">
                  <span v-if="item.code" class="text-blue-600 font-mono text-xs">[{{ item.code }}]</span>
                  <span class="font-semibold text-slate-800 text-sm sm:text-base">{{ item.name }}</span>
                  <span
                    class="px-1.5 py-0.5 rounded text-[11px] font-medium"
                    :class="getSourceBadgeClass(item.source_type, item.source_status)"
                  >
                    {{ getSourceBadgeLabel(item.source_type, item.source_status) }}
                  </span>
                </div>
                <div class="text-xs text-slate-400 mt-1 flex items-center gap-2">
                  <span>{{ item.chapter || '未分类章节' }}</span>
                  <span v-if="item.source_reference" class="text-slate-400 font-normal">
                    · {{ item.source_reference }}
                  </span>
                </div>
              </td>

              <!-- Priority Selector -->
              <td class="py-3 px-3 text-center">
                <select
                  v-model="item.priority_tag"
                  @change="updatePriorityTag(item)"
                  class="text-xs font-medium rounded-lg px-2 py-1 border transition cursor-pointer"
                  :class="priorityTagStyle(item.priority_tag)"
                >
                  <option value="important">重点背 ★</option>
                  <option value="normal">普通</option>
                  <option value="deferred">暂缓</option>
                </select>
              </td>

              <!-- Total Questions -->
              <td class="py-3 px-3 text-center font-bold text-slate-800">
                <span v-if="item.questions_count > 0">{{ item.questions_count }} 道</span>
                <span v-else class="text-xs text-slate-400 font-normal">暂无考查记录</span>
              </td>

              <!-- Years Count -->
              <td class="py-3 px-3 text-center font-semibold" :class="item.years_count > 0 ? 'text-blue-700' : 'text-slate-400 font-normal'">
                {{ item.years_count > 0 ? `${item.years_count} 年` : '-' }}
              </td>

              <!-- Recent 5-Year Window -->
              <td class="py-3 px-3 text-center">
                <span
                  v-if="overallStats.recent_5yr_window.length > 0"
                  class="px-2 py-0.5 rounded text-xs font-semibold"
                  :class="item.recent_5yr_count >= 2 ? 'bg-rose-50 text-rose-700' : (item.recent_5yr_count === 1 ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-400')"
                >
                  {{ item.recent_5yr_count }} / {{ overallStats.recent_5yr_window.length }} 年
                </span>
                <span v-else class="text-xs text-slate-400">-</span>
              </td>

              <!-- Recent Year -->
              <td class="py-3 px-3 text-center text-slate-600">
                <span v-if="item.recent_year" class="font-medium text-slate-700">{{ item.recent_year }} 年</span>
                <span v-else class="text-slate-300 text-xs italic">未曾考查</span>
              </td>

              <!-- Actions -->
              <td class="py-3 px-4 text-right space-x-1.5 whitespace-nowrap">
                <button
                  v-if="item.questions.length > 0"
                  type="button"
                  class="btn btn-sm btn-secondary"
                  @click="viewQuestionsDetail(item)"
                >
                  原题 ({{ item.questions.length }})
                </button>
                <router-link
                  :to="`/recall?chapter_id=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-primary"
                >
                  回忆卡
                </router-link>
                <router-link
                  :to="`/today?chapter_id=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-secondary"
                >
                  今日学习
                </router-link>
                <router-link
                  :to="`/cards?kpid=${encodeURIComponent(item.id)}`"
                  class="btn btn-sm btn-secondary"
                >
                  考点讲义
                </router-link>
                <button
                  type="button"
                  class="p-1.5 text-slate-400 hover:text-rose-600 rounded"
                  title="删除考点"
                  @click="handleDeleteKp(item.id, item.name)"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Extraction Wizard Modal -->
    <ExtractionModal
      :show="isExtractionModalOpen"
      @close="isExtractionModalOpen = false"
      @success="handleExtractionSuccess"
    />

    <!-- Add Knowledge Point Modal -->
    <BaseModal
      :show="isAddModalOpen"
      title="新增知识点"
      max-width="md"
      @close="isAddModalOpen = false"
    >
      <div class="space-y-3.5 text-sm">
        <div>
          <label class="field-label">知识点名称</label>
          <input v-model="newKpForm.name" type="text" placeholder="如: 目标管理理论 (MBO)" class="field" />
        </div>
        <div>
          <label class="field-label">章节序号 / 编号 (选填)</label>
          <input v-model="newKpForm.code" type="text" placeholder="如: 3.2" class="field" />
        </div>
        <div>
          <label class="field-label">所属章节</label>
          <input v-model="newKpForm.chapter" type="text" placeholder="如: 第三章 组织与设计" class="field" />
        </div>
        <div>
          <label class="field-label">背诵优先级</label>
          <select v-model="newKpForm.priority_tag" class="field">
            <option value="important">重点背 ★</option>
            <option value="normal">普通</option>
            <option value="deferred">暂缓</option>
          </select>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isAddModalOpen = false">取消</button>
        <button type="button" class="btn btn-primary" @click="handleAddKp">保存考点</button>
      </template>
    </BaseModal>

    <!-- Merge Synonymous Modal -->
    <BaseModal
      :show="isMergeModalOpen"
      title="合并同义考点"
      max-width="lg"
      @close="isMergeModalOpen = false"
    >
      <div class="space-y-4 text-sm">
        <p class="text-xs text-slate-500 leading-relaxed">
          选择一个保留的目标知识点，勾选要合并吸纳的同义考点。合并后历史真题关联与模拟练习将平滑转移至目标考点，原考点将被软删除以防断链。
        </p>
        <div>
          <label class="field-label">保留的目标考点 (最终名称)</label>
          <select v-model="mergeTargetId" class="field">
            <option disabled value="">-- 请选择保留的目标考点 --</option>
            <option v-for="kp in pointStats" :key="kp.id" :value="kp.id">
              {{ kp.name }} ({{ kp.chapter || '未分类' }})
            </option>
          </select>
        </div>

        <div>
          <label class="field-label">被合并吸收的同义考点 (多选)</label>
          <div class="max-h-48 overflow-y-auto space-y-1.5 border border-slate-200 p-2.5 rounded-xl bg-slate-50/50">
            <label
              v-for="kp in pointStats.filter(p => p.id !== mergeTargetId)"
              :key="kp.id"
              class="flex items-center gap-2 p-1.5 hover:bg-white rounded cursor-pointer text-xs"
            >
              <input type="checkbox" :value="kp.id" v-model="mergeSourceIds" class="rounded text-blue-600" />
              <span class="text-slate-800 font-medium">{{ kp.name }}</span>
              <span class="text-slate-400">({{ kp.questions_count }} 道题)</span>
            </label>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isMergeModalOpen = false">取消</button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="!mergeTargetId || mergeSourceIds.length === 0"
          @click="handleMergeKp"
        >
          确认合并
        </button>
      </template>
    </BaseModal>

    <!-- Legacy Knowledge Points Modal -->
    <BaseModal
      :show="isLegacyModalOpen"
      title="管理待核实旧考点"
      max-width="2xl"
      @close="isLegacyModalOpen = false"
    >
      <div class="space-y-3.5 text-sm">
        <p class="text-xs text-slate-500 leading-relaxed">
          以下为系统初始化保留的历史考点（来源标记为待核实）。您可以在此逐一审核：核对无误可标记为有效，或将其删除。
        </p>
        <div v-if="legacyKPs.length === 0" class="py-8 text-center text-slate-400 text-xs">
          没有待核实的旧考点记录。
        </div>
        <div v-else class="space-y-2 max-h-60 overflow-y-auto pr-1">
          <div
            v-for="kp in legacyKPs"
            :key="kp.id"
            class="p-3 rounded-xl border border-slate-200 bg-slate-50/50 flex items-center justify-between gap-3 text-xs"
          >
            <div>
              <span class="font-semibold text-slate-800 text-sm">{{ kp.name }}</span>
              <span v-if="kp.chapter" class="text-slate-500 ml-2">({{ kp.chapter }})</span>
            </div>
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="btn btn-sm btn-primary bg-emerald-600 hover:bg-emerald-700"
                @click="verifyLegacyKp(kp.id)"
              >
                标记已核实
              </button>
              <button
                type="button"
                class="btn btn-sm btn-secondary text-rose-600 hover:bg-rose-50 border-rose-200"
                @click="handleDeleteKp(kp.id, kp.name)"
              >
                删除
              </button>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary" @click="isLegacyModalOpen = false">关闭</button>
      </template>
    </BaseModal>

    <!-- Questions Detail Modal -->
    <BaseModal
      :show="selectedKpDetail !== null"
      :title="`历年真题与答案: ${selectedKpDetail?.name || ''}`"
      max-width="3xl"
      @close="selectedKpDetail = null"
    >
      <div v-if="selectedKpDetail" class="space-y-3.5 text-sm">
        <div v-if="selectedKpDetail.questions.length === 0" class="py-8 text-center text-slate-400 text-xs">
          该知识点在已确认真题中暂无考查记录。
        </div>
        <div
          v-for="q in selectedKpDetail.questions"
          :key="q.id"
          class="surface p-4 space-y-2 text-xs sm:text-sm"
        >
          <div class="flex items-center justify-between text-xs border-b border-slate-100 pb-2">
            <span class="font-semibold text-blue-700">
              {{ q.year ? `${q.year}年真题` : '历年真题' }} · {{ q.session_name || '' }} {{ q.question_num || '' }}
            </span>
            <span v-if="q.score" class="text-slate-400">[{{ q.score }} 分]</span>
          </div>
          <p class="reading-content font-medium text-slate-900">
            {{ q.stem }}
          </p>
          <div class="bg-slate-50 p-3 rounded-lg border border-slate-100 text-xs space-y-1">
            <span class="font-semibold text-slate-600 block">参考答案：</span>
            <p class="text-slate-700 whitespace-pre-wrap leading-relaxed">
              {{ q.standard_answer || '[暂未收录参考答案]' }}
            </p>
          </div>
        </div>
      </div>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue';
import {
  Sparkles, RotateCw, Plus, Merge, Calendar,
  Search, Trash2, HelpCircle
} from 'lucide-vue-next';
import { apiClient } from '../api/client';
import type { KnowledgePointStats, OverallStats, KnowledgePoint } from '../types';
import ExtractionModal from '../components/ExtractionModal.vue';
import RunningTasksPanel from '../components/RunningTasksPanel.vue';
import BaseModal from '../components/BaseModal.vue';
import EmptyState from '../components/EmptyState.vue';

const overallStats = ref<OverallStats>({
  total_recorded_years: 0,
  min_year: null,
  max_year: null,
  recorded_years: [],
  missing_years: [],
  recent_5yr_window: [],
  total_questions: 0,
  confirmed_questions: 0,
  knowledge_points_count: 0
});

const pointStats = ref<KnowledgePointStats[]>([]);
const legacyKPs = ref<KnowledgePoint[]>([]);
const selectedPriority = ref<string>('all');
const searchKeyword = ref('');
const selectedChapter = ref('');
const selectedKpDetail = ref<KnowledgePointStats | null>(null);

const isRefreshing = ref(false);
const lastRefreshTime = ref('');
const showMoreMenu = ref(false);

const isExtractionModalOpen = ref(false);
const isAddModalOpen = ref(false);
const isMergeModalOpen = ref(false);
const isLegacyModalOpen = ref(false);

const newKpForm = ref({
  name: '',
  code: '',
  chapter: '',
  priority_tag: 'important' as 'important' | 'normal' | 'deferred'
});

const mergeTargetId = ref('');
const mergeSourceIds = ref<string[]>([]);

const priorityTabs = [
  { label: '全部考点', value: 'all' },
  { label: '重点背 ★', value: 'important' },
  { label: '普通', value: 'normal' },
  { label: '暂缓', value: 'deferred' }
];

const legacyCount = computed(() => legacyKPs.value.length);

const chaptersList = computed(() => {
  const chapters = new Set<string>();
  for (const p of pointStats.value) {
    if (p.chapter) chapters.add(p.chapter.trim());
  }
  return Array.from(chapters);
});

const filteredPointStats = computed(() => {
  return pointStats.value.filter(p => {
    if (selectedPriority.value !== 'all' && p.priority_tag !== selectedPriority.value) {
      return false;
    }
    if (selectedChapter.value && p.chapter?.trim() !== selectedChapter.value) {
      return false;
    }
    if (searchKeyword.value.trim()) {
      const q = searchKeyword.value.trim().toLowerCase();
      const matchName = p.name.toLowerCase().includes(q);
      const matchChapter = p.chapter?.toLowerCase().includes(q);
      const matchCode = p.code?.toLowerCase().includes(q);
      if (!matchName && !matchChapter && !matchCode) return false;
    }
    return true;
  });
});

const fetchAll = async () => {
  try {
    isRefreshing.value = true;
    const [overall, list, legacy] = await Promise.all([
      apiClient.getOverallStats(),
      apiClient.getKnowledgePointsStats(),
      apiClient.getLegacyKnowledgePoints()
    ]);
    overallStats.value = overall;
    pointStats.value = list;
    legacyKPs.value = legacy;
    const now = new Date();
    lastRefreshTime.value = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;
  } catch (e) {
    console.error('Failed to load stats:', e);
  } finally {
    isRefreshing.value = false;
  }
};

const handleRefresh = async () => {
  await fetchAll();
};

const handleExtractionSuccess = async () => {
  await fetchAll();
};

const getSourceBadgeLabel = (sourceType?: string, sourceStatus?: string) => {
  if (sourceStatus === 'supplement_needed') return '依据待补';
  switch (sourceType) {
    case 'extracted': return '资料提取';
    case 'pending_verification': return '待核实来源';
    default: return '手动录入';
  }
};

const getSourceBadgeClass = (sourceType?: string, sourceStatus?: string) => {
  if (sourceStatus === 'supplement_needed') return 'bg-rose-100 text-rose-800';
  switch (sourceType) {
    case 'extracted': return 'bg-blue-100 text-blue-800';
    case 'pending_verification': return 'bg-amber-100 text-amber-800';
    default: return 'bg-slate-100 text-slate-700';
  }
};

const priorityTagStyle = (tag: string) => {
  if (tag === 'important') return 'bg-rose-50 text-rose-700 border-rose-200';
  if (tag === 'deferred') return 'bg-slate-100 text-slate-500 border-slate-200';
  return 'bg-blue-50 text-blue-700 border-blue-200';
};

const updatePriorityTag = async (item: KnowledgePointStats) => {
  try {
    await apiClient.updateKnowledgePoint(item.id, { priority_tag: item.priority_tag });
  } catch (err: any) {
    alert('更新优先级失败: ' + err.message);
  }
};

const viewQuestionsDetail = (item: KnowledgePointStats) => {
  selectedKpDetail.value = item;
};

const openAddKpModal = () => {
  newKpForm.value = { name: '', code: '', chapter: '', priority_tag: 'important' };
  isAddModalOpen.value = true;
};

const handleAddKp = async () => {
  if (!newKpForm.value.name.trim()) {
    alert('请输入知识点名称');
    return;
  }
  try {
    await apiClient.createKnowledgePoint({
      name: newKpForm.value.name.trim(),
      code: newKpForm.value.code.trim() || undefined,
      chapter: newKpForm.value.chapter.trim() || undefined,
      priority_tag: newKpForm.value.priority_tag,
      sort_order: 0
    });
    isAddModalOpen.value = false;
    await fetchAll();
  } catch (err: any) {
    alert('创建失败: ' + err.message);
  }
};

const openMergeModal = () => {
  mergeTargetId.value = '';
  mergeSourceIds.value = [];
  isMergeModalOpen.value = true;
};

const handleMergeKp = async () => {
  if (!mergeTargetId.value || mergeSourceIds.value.length === 0) return;
  try {
    await apiClient.mergeKnowledgePoints(mergeTargetId.value, mergeSourceIds.value);
    isMergeModalOpen.value = false;
    await fetchAll();
  } catch (err: any) {
    alert('合并失败: ' + err.message);
  }
};

const openLegacyModal = () => {
  isLegacyModalOpen.value = true;
};

const verifyLegacyKp = async (id: string) => {
  try {
    await apiClient.updateKnowledgePoint(id, { source_type: 'manual', source_status: 'valid' });
    await fetchAll();
  } catch (err: any) {
    alert('更新失败: ' + err.message);
  }
};

const handleDeleteKp = async (id: string, name: string) => {
  if (!confirm(`确定要删除考点「${name}」吗？历史练习记录将被安全保留，不会断链。`)) return;
  try {
    await apiClient.deleteKnowledgePoint(id);
    await fetchAll();
  } catch (err: any) {
    alert('删除失败: ' + err.message);
  }
};

onMounted(() => {
  fetchAll();
});
</script>
