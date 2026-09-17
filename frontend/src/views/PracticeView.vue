<template>
  <div class="page-stack">
    <!-- Header & Generator Controls -->
    <div class="surface p-5 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-stone-100 pb-4">
        <div>
          <h2 class="font-display text-base sm:text-lg font-bold text-stone-900 tracking-tight">
            真题风格模拟练习与多维度智能批改
          </h2>
          <p class="text-xs text-stone-500 mt-0.5">
            AI 仿照真题出题（标记为模拟题，不计入真题统计） · 作答前隐藏标准答案 · 逐项覆盖诊断 · 概念纠错
          </p>
        </div>

        <div class="flex items-center gap-2">
          <button
            type="button"
            class="btn text-xs"
            :class="activeTab === 'new_practice' ? 'btn-primary' : 'btn-secondary'"
            @click="activeTab = 'new_practice'"
          >
            开始模拟练习
          </button>
          <button
            type="button"
            class="btn text-xs"
            :class="activeTab === 'history' ? 'btn-primary' : 'btn-secondary'"
            @click="activeTab = 'history'"
          >
            练习历史与复盘 ({{ sessions.length }})
          </button>
        </div>
      </div>

      <!-- Generator Options (Active when in new_practice mode and not in session) -->
      <div v-if="activeTab === 'new_practice' && !currentSession" class="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs sm:text-sm items-end">
        <div>
          <label class="field-label">针对考点：</label>
          <select v-model="generateForm.knowledge_point_id" class="field text-xs sm:text-sm">
            <option disabled value="">-- 选择考点 --</option>
            <option v-for="kp in knowledgePoints" :key="kp.id" :value="kp.id">
              {{ kp.name }} ({{ kp.chapter || '未分类' }})
            </option>
          </select>
        </div>

        <div>
          <label class="field-label">题量 (1~5 道)：</label>
          <select v-model.number="generateForm.question_count" class="field text-xs sm:text-sm">
            <option :value="1">1 道题 (快速检验)</option>
            <option :value="2">2 道题 (针对巩固)</option>
            <option :value="3">3 道题 (全面覆盖)</option>
            <option :value="5">5 道题 (专项模拟)</option>
          </select>
        </div>

        <div>
          <label class="field-label">考查难度：</label>
          <select v-model="generateForm.difficulty" class="field text-xs sm:text-sm">
            <option value="basic">基础考查 (概念内涵与主要特征)</option>
            <option value="comprehensive">综合应用 (情景对比与适用条件)</option>
          </select>
        </div>

        <div>
          <button
            type="button"
            class="btn btn-primary w-full"
            :disabled="!generateForm.knowledge_point_id || isGenerating"
            @click="handleGeneratePractice"
          >
            <Loader2 v-if="isGenerating" class="w-4 h-4 mr-1.5 animate-spin" />
            <span>{{ isGenerating ? '正在智能命题中...' : '生成模拟试卷' }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Active Practice Session Form (Answering & Evaluation) -->
    <div v-if="activeTab === 'new_practice' && currentSession" class="space-y-6">
      <!-- Session Bar -->
      <div class="page-toolbar">
        <div class="flex items-center gap-2.5 flex-wrap text-xs sm:text-sm">
          <span class="px-2 py-0.5 rounded bg-purple-100 text-purple-800 font-bold text-xs">
            模拟试卷
          </span>
          <span class="font-bold text-stone-800">考点：{{ currentSession.knowledge_point_name }}</span>
          <span class="text-stone-300">·</span>
          <span class="text-stone-500">难度：{{ currentSession.difficulty === 'basic' ? '基础概念' : '综合应用' }}</span>
          <span class="text-stone-300">·</span>
          <span class="text-stone-500">题量：{{ currentSession.questions.length }} 道</span>
        </div>

        <button
          v-if="currentSession.evaluation"
          type="button"
          class="btn btn-sm btn-secondary"
          @click="startNewExercise"
        >
          <RotateCcw class="w-3.5 h-3.5 mr-1" />
          重新出题
        </button>
      </div>

      <!-- Questions List -->
      <div
        v-for="(q, idx) in currentSession.questions"
        :key="q.question_id"
        class="surface p-5 sm:p-6 space-y-4"
      >
        <!-- Question Stem -->
        <div class="flex items-start gap-3">
          <span class="px-2 py-0.5 rounded bg-brand-50 text-brand-700 font-bold text-xs shrink-0 mt-0.5">
            第 {{ idx + 1 }} 题
          </span>
          <p class="reading-content font-semibold text-stone-900 leading-relaxed">
            {{ q.stem }}
          </p>
        </div>

        <!-- Student Answer Textarea (Before Submission) -->
        <div v-if="!currentSession.evaluation" class="space-y-1.5">
          <label class="field-label">作答区域 (自动保存草稿)：</label>
          <textarea
            v-model="answersMap[q.question_id]"
            @input="saveDraft"
            rows="6"
            placeholder="根据专业课规范，分点论述核心概念定义、主要特征要素、运行机制及适用条件..."
            class="field font-mono text-xs sm:text-sm p-3.5 leading-relaxed bg-stone-50/40"
          />
        </div>

        <!-- Evaluation Diagnosis (After Submission) -->
        <div v-else class="space-y-4 pt-3 border-t border-stone-100">
          <!-- Student's submitted text -->
          <div class="bg-stone-50 p-4 rounded-xl border border-stone-200 text-xs sm:text-sm">
            <span class="font-semibold text-stone-700 block mb-1">考生作答内容：</span>
            <p class="text-stone-700 whitespace-pre-wrap leading-relaxed">{{ getStudentAnswer(q.question_id) || '[考生未作答]' }}</p>
          </div>

          <!-- Evaluation Diagnosis for this question -->
          <div v-if="getQuestionEval(q.question_id)" class="space-y-3.5">
            <!-- Score & Rubric Banner -->
            <div class="surface p-4 flex flex-wrap items-center justify-between gap-2 text-xs sm:text-sm bg-stone-50/70">
              <div class="flex items-center gap-2">
                <span class="font-bold text-stone-700">参考估分：</span>
                <span v-if="getQuestionEval(q.question_id)?.score_estimate !== null" class="text-base font-bold text-brand-700">
                  {{ getQuestionEval(q.question_id)?.score_estimate }} / {{ getQuestionEval(q.question_id)?.max_score || 10 }} 分
                </span>
                <span v-else class="text-stone-400 italic text-xs">
                  [按要点覆盖评定，不虚构考试分数]
                </span>
              </div>
              <span class="text-xs text-stone-500">
                {{ getQuestionEval(q.question_id)?.score_basis }}
              </span>
            </div>

            <!-- Itemized Rubric Checklist -->
            <div class="space-y-2">
              <h4 class="text-xs font-bold text-stone-700 flex items-center gap-1.5">
                <CheckSquare class="w-4 h-4 text-brand-600" />
                得分要点逐项核对（同义表达均予认定）：
              </h4>

              <div class="space-y-2">
                <div
                  v-for="(kpCov, cIdx) in getQuestionEval(q.question_id)?.keypoints_coverage"
                  :key="cIdx"
                  class="p-3.5 rounded-xl border text-xs sm:text-sm space-y-1"
                  :class="coverageItemStyle(kpCov.status)"
                >
                  <div class="flex items-center justify-between font-semibold">
                    <span class="text-stone-800">要点 {{ cIdx + 1 }}: {{ kpCov.point }}</span>
                    <span class="px-2 py-0.5 rounded text-[11px] font-bold" :class="coverageBadgeStyle(kpCov.status)">
                      {{ formatCoverageStatus(kpCov.status) }}
                    </span>
                  </div>
                  <p class="text-stone-600 leading-relaxed">{{ kpCov.detail }}</p>
                  <span class="text-xs text-stone-400 block">依据: {{ kpCov.basis }}</span>
                </div>
              </div>
            </div>

            <!-- Conceptual Errors & Ungrounded Warnings -->
            <div
              v-if="getQuestionEval(q.question_id)?.conceptual_errors?.length"
              class="bg-rose-50 border border-rose-200 rounded-xl p-3.5 text-xs sm:text-sm text-rose-800 space-y-1.5"
            >
              <span class="font-bold flex items-center gap-1.5">
                <AlertCircle class="w-4 h-4 text-rose-600" />
                概念错误与混淆预警：
              </span>
              <ul class="list-disc list-inside space-y-0.5 text-rose-700 pl-1">
                <li v-for="(err, eIdx) in getQuestionEval(q.question_id)?.conceptual_errors" :key="eIdx">
                  {{ err }}
                </li>
              </ul>
            </div>

            <div
              v-if="getQuestionEval(q.question_id)?.ungrounded_claims?.length"
              class="bg-amber-50 border border-amber-200 rounded-xl p-3.5 text-xs sm:text-sm text-amber-800 space-y-1.5"
            >
              <span class="font-bold flex items-center gap-1.5">
                <AlertTriangle class="w-4 h-4 text-amber-600" />
                无理论依据的表述：
              </span>
              <ul class="list-disc list-inside space-y-0.5 text-amber-700 pl-1">
                <li v-for="(claim, cIdx) in getQuestionEval(q.question_id)?.ungrounded_claims" :key="cIdx">
                  {{ claim }}
                </li>
              </ul>
            </div>

            <!-- Expression Rewrite Advice -->
            <div class="bg-brand-50/40 border border-brand-100 rounded-xl p-4 text-xs sm:text-sm text-brand-900 space-y-1">
              <span class="font-bold block text-brand-950">高分规范答题改写建议：</span>
              <p class="text-brand-900 leading-relaxed whitespace-pre-wrap">{{ getQuestionEval(q.question_id)?.expression_rewrite }}</p>
            </div>

            <!-- Standard Reference Answer (Revealed after submission) -->
            <div class="surface p-4 space-y-1 bg-stone-50/50">
              <span class="font-bold text-stone-700 block">参考标准答案：</span>
              <p class="text-stone-700 whitespace-pre-wrap leading-relaxed">{{ q.reference_answer }}</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Submit Action Button -->
      <div v-if="!currentSession.evaluation" class="flex justify-end">
        <button
          type="button"
          class="btn btn-primary px-6 py-2.5 text-sm"
          :disabled="isSubmitting"
          @click="checkAndSubmit"
        >
          <Loader2 v-if="isSubmitting" class="w-4 h-4 mr-1.5 animate-spin" />
          <span>{{ isSubmitting ? '正在逐项多维度智能批改中...' : '提交作答并获得批改诊断' }}</span>
        </button>
      </div>
    </div>

    <!-- History Tab -->
    <div v-if="activeTab === 'history'" class="space-y-4">
      <div v-if="sessions.length === 0">
        <EmptyState title="暂无模拟练习记录" description="点击上方“开始模拟练习”，检验对核心考点的掌握程度与作答规范。" />
      </div>
      <div v-else class="space-y-3">
        <div
          v-for="sess in sessions"
          :key="sess.id"
          class="surface p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs sm:text-sm"
        >
          <div>
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-bold text-stone-800 text-sm sm:text-base">{{ sess.knowledge_point_name }}</span>
              <span class="px-2 py-0.5 rounded bg-brand-50 text-brand-700 font-medium text-xs">
                {{ sess.question_count }} 题 ({{ sess.difficulty === 'basic' ? '基础' : '综合' }})
              </span>
              <span
                class="px-2 py-0.5 rounded text-xs font-semibold"
                :class="sess.evaluation ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'"
              >
                {{ sess.evaluation ? '已完成批改' : '待作答' }}
              </span>
            </div>
            <p class="text-stone-400 text-xs mt-1">创建时间: {{ formatDate(sess.created_at) }}</p>
          </div>

          <div class="flex items-center gap-2 self-end sm:self-center">
            <button
              type="button"
              class="btn btn-sm btn-secondary"
              @click="loadSessionIntoView(sess)"
            >
              查看作答与批改报告
            </button>
            <button
              type="button"
              class="btn btn-sm btn-primary"
              @click="rePracticeKnowledgePoint(sess.knowledge_point_id)"
            >
              再次强化该考点
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Blank Submission Confirmation Modal -->
    <BaseModal
      :show="showBlankConfirm"
      title="确认提交"
      max-width="sm"
      @close="showBlankConfirm = false"
    >
      <div class="space-y-2 text-sm text-stone-600">
        <p>检测到部分或全部题目作答为空。确认直接提交进行批改诊断吗？</p>
        <p class="text-xs text-stone-400">
          未作答的题目将被判定为缺失，系统将展示该题的标准答案与答题框架。
        </p>
      </div>
      <template #footer>
        <button type="button" class="btn btn-secondary text-xs" @click="showBlankConfirm = false">继续作答</button>
        <button type="button" class="btn btn-primary text-xs" @click="confirmSubmit">确认提交</button>
      </template>
    </BaseModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { Loader2, RotateCcw, CheckSquare, AlertCircle, AlertTriangle } from 'lucide-vue-next';
import { apiClient, sessionTransport } from '../api/client';
import type { KnowledgePoint, PracticeSession, QuestionEvaluation } from '../types';
import BaseModal from '../components/BaseModal.vue';
import EmptyState from '../components/EmptyState.vue';

const activeTab = ref<'new_practice' | 'history'>('new_practice');
const knowledgePoints = ref<KnowledgePoint[]>([]);
const sessions = ref<PracticeSession[]>([]);

const generateForm = ref({
  knowledge_point_id: '',
  question_count: 1,
  difficulty: 'basic' as 'basic' | 'comprehensive'
});

const isGenerating = ref(false);
const isSubmitting = ref(false);
const currentSession = ref<PracticeSession | null>(null);
const answersMap = ref<Record<string, string>>({});
const showBlankConfirm = ref(false);
let generateKey: string | null = null;
let generateFingerprint = '';
let submitKey: string | null = null;
let submitFingerprint = '';

const fetchKnowledgePoints = async () => {
  try {
    knowledgePoints.value = await apiClient.listKnowledgePoints();
    if (knowledgePoints.value.length > 0 && !generateForm.value.knowledge_point_id) {
      generateForm.value.knowledge_point_id = knowledgePoints.value[0].id;
    }
  } catch (e) {
    console.error(e);
  }
};

const fetchSessions = async () => {
  try {
    sessions.value = await apiClient.listPracticeSessions();
  } catch (e) {
    console.error(e);
  }
};

const handleGeneratePractice = async () => {
  if (!generateForm.value.knowledge_point_id) return;
  isGenerating.value = true;
  const payload = { knowledge_point_id: generateForm.value.knowledge_point_id, question_count: generateForm.value.question_count, difficulty: generateForm.value.difficulty };
  const fingerprint = JSON.stringify(payload);
  if (fingerprint !== generateFingerprint) { generateFingerprint = fingerprint; generateKey = null; }
  generateKey ??= sessionTransport.idempotencyKey('practice-generate', payload);
  try {
    const session = await apiClient.generatePractice(
      generateForm.value.knowledge_point_id,
      generateForm.value.question_count,
      generateForm.value.difficulty,
      generateKey
    );
    currentSession.value = session;
    answersMap.value = {};
    for (const q of session.questions) {
      answersMap.value[q.question_id] = '';
    }
    await fetchSessions();
    sessionTransport.clearIdempotencyKey('practice-generate', payload);
    generateKey = null;
    generateFingerprint = '';
  } catch (err: any) {
    if (!sessionTransport.isCancelled(err)) alert(
      ['ECONNABORTED', 'ERR_NETWORK'].includes(err.code)
        ? '连接中断，后台可能仍在出题。保持选择不变，再次点击生成可获取同一次结果。'
        : '出题失败: ' + err.message);
  } finally {
    isGenerating.value = false;
  }
};

const saveDraft = () => {
  if (!currentSession.value) return;
  try {
    localStorage.setItem(`practice_draft_${currentSession.value.id}`, JSON.stringify(answersMap.value));
  } catch (e) {
    // Ignore localStorage errors
  }
};

const checkAndSubmit = () => {
  const hasEmpty = Object.values(answersMap.value).some(a => !a.trim());
  if (hasEmpty) {
    showBlankConfirm.value = true;
  } else {
    executeSubmit();
  }
};

const confirmSubmit = () => {
  showBlankConfirm.value = false;
  executeSubmit();
};

const executeSubmit = async () => {
  if (!currentSession.value) return;
  isSubmitting.value = true;
  try {
    const submissions = Object.entries(answersMap.value).map(([qid, text]) => ({
      question_id: qid,
      answer_text: text
    }));
    const fingerprint = JSON.stringify({ session_id: currentSession.value.id, answers: submissions });
    if (fingerprint !== submitFingerprint) { submitFingerprint = fingerprint; submitKey = null; }
    submitKey ??= sessionTransport.idempotencyKey('practice-submit', { session_id: currentSession.value.id, answers: submissions });
    const evaluated = await apiClient.submitPractice(currentSession.value.id, submissions, submitKey);
    currentSession.value = evaluated;
    // Clear draft
    localStorage.removeItem(`practice_draft_${currentSession.value.id}`);
    await fetchSessions();
    sessionTransport.clearIdempotencyKey('practice-submit', { session_id: currentSession.value.id, answers: submissions });
    submitKey = null;
    submitFingerprint = '';
  } catch (err: any) {
    if (!sessionTransport.isCancelled(err)) alert(
      ['ECONNABORTED', 'ERR_NETWORK'].includes(err.code)
        ? '连接中断，答案已保留。保持答案不变，再次提交可获取同一次批改结果。'
        : '批改提交失败: ' + err.message);
  } finally {
    isSubmitting.value = false;
  }
};

const getStudentAnswer = (qid: string): string => {
  if (!currentSession.value?.user_answers) return answersMap.value[qid] || '';
  const item = currentSession.value.user_answers.find(a => a.question_id === qid);
  return item ? item.answer_text : '';
};

const getQuestionEval = (qid: string): QuestionEvaluation | undefined => {
  if (!currentSession.value?.evaluation) return undefined;
  return currentSession.value.evaluation.find(e => e.question_id === qid);
};

const coverageItemStyle = (status: string) => {
  if (status === 'COVERED') return 'bg-emerald-50/40 border-emerald-200';
  if (status === 'PARTIALLY_COVERED') return 'bg-amber-50/40 border-amber-200';
  return 'bg-rose-50/40 border-rose-200';
};

const coverageBadgeStyle = (status: string) => {
  if (status === 'COVERED') return 'bg-emerald-100 text-emerald-800';
  if (status === 'PARTIALLY_COVERED') return 'bg-amber-100 text-amber-800';
  return 'bg-rose-100 text-rose-800';
};

const formatCoverageStatus = (status: string) => {
  if (status === 'COVERED') return '✓ 已覆盖';
  if (status === 'PARTIALLY_COVERED') return '▲ 部分覆盖';
  return '✗ 缺失';
};

const startNewExercise = () => {
  currentSession.value = null;
  answersMap.value = {};
  submitKey = null;
  submitFingerprint = '';
};

const loadSessionIntoView = (sess: PracticeSession) => {
  currentSession.value = sess;
  activeTab.value = 'new_practice';
  answersMap.value = {};
  if (sess.user_answers) {
    for (const a of sess.user_answers) {
      answersMap.value[a.question_id] = a.answer_text;
    }
  } else {
    // Check if there is a draft in localStorage
    try {
      const saved = localStorage.getItem(`practice_draft_${sess.id}`);
      if (saved) answersMap.value = JSON.parse(saved);
    } catch (e) {
      // Ignore
    }
  }
};

const rePracticeKnowledgePoint = (kpid: string) => {
  generateForm.value.knowledge_point_id = kpid;
  startNewExercise();
  activeTab.value = 'new_practice';
};

const formatDate = (iso: string) => {
  if (!iso) return '';
  return iso.replace('T', ' ').substring(0, 16);
};

onMounted(() => {
  fetchKnowledgePoints();
  fetchSessions();
});
</script>
