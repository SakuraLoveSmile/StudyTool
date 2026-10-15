<template>
  <BaseModal
    :show="show"
    title="从资料提取考点"
    max-width="3xl"
    :close-on-backdrop="step !== 'processing'"
    @close="handleClose"
  >
    <!-- Step Indicator -->
    <div class="mb-6">
      <div class="flex items-center justify-between text-xs font-medium text-sub max-w-lg mx-auto">
        <div class="flex items-center gap-1.5" :class="{ 'text-accent font-semibold': step === 'select' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'select' ? 'border-accent bg-accent/10 text-accent' : 'border-line-strong'">1</span>
          <span>选择资料</span>
        </div>
        <div class="w-8 h-px bg-raised" />
        <div class="flex items-center gap-1.5" :class="{ 'text-accent font-semibold': step === 'processing' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'processing' ? 'border-accent bg-accent/10 text-accent' : 'border-line-strong'">2</span>
          <span>AI 梳理</span>
        </div>
        <div class="w-8 h-px bg-raised" />
        <div class="flex items-center gap-1.5" :class="{ 'text-accent font-semibold': step === 'review' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'review' ? 'border-accent bg-accent/10 text-accent' : 'border-line-strong'">3</span>
          <span>核对候选</span>
        </div>
        <div class="w-8 h-px bg-raised" />
        <div class="flex items-center gap-1.5" :class="{ 'text-accent font-semibold': step === 'linking' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'linking' ? 'border-accent bg-accent/10 text-accent' : 'border-line-strong'">4</span>
          <span>真题关联</span>
        </div>
        <div class="w-8 h-px bg-raised" />
        <div class="flex items-center gap-1.5" :class="{ 'text-ok font-semibold': step === 'done' }">
          <span class="w-5 h-5 rounded-full flex items-center justify-center border text-[11px]" :class="step === 'done' ? 'border-ok bg-ok/10 text-ok' : 'border-line-strong'">5</span>
          <span>完成</span>
        </div>
      </div>
    </div>

    <!-- Step 1: Select Documents -->
    <div v-if="step === 'select'" class="space-y-4">
      <!-- Resume banner: previous extraction still running or awaiting review -->
      <div
        v-if="showResumeBanner"
        class="p-3 rounded-xl border border-accent/30 bg-accent/5 flex items-center justify-between gap-3 text-xs"
      >
        <div class="space-y-0.5 min-w-0">
          <p class="font-semibold text-ink">
            {{ isBatchRunning(pendingBatch) ? '上一次考点提取仍在进行中' : `上次提取还有 ${pendingBatch?.pending_candidates ?? 0} 个考点候选未核对` }}
          </p>
          <p class="text-faint">
            {{ isBatchRunning(pendingBatch) ? '可继续等待任务完成，完成后直接进入核对环节。' : '继续核对可将候选考点入库到考点目录。' }}
          </p>
        </div>
        <button
          type="button"
          class="btn btn-sm btn-primary shrink-0"
          @click="resumePendingBatch"
        >
          {{ isBatchRunning(pendingBatch) ? '继续等待' : '继续核对' }}
        </button>
      </div>

      <p class="text-sm text-sub">
        请选择用于提取考点的参考教材或补充资料。系统将分析资料切片并梳理核心概念。
      </p>

      <section class="surface p-3 space-y-2" aria-labelledby="extraction-batches-title">
        <div class="flex items-center justify-between gap-2">
          <h4 id="extraction-batches-title" class="text-sm font-semibold text-ink">可恢复的提取批次</h4>
          <button type="button" class="btn btn-sm btn-secondary" :disabled="loadingBatches" @click="() => loadBatches()">
            {{ loadingBatches ? '加载中...' : '刷新' }}
          </button>
        </div>
        <p v-if="batchError" class="text-xs text-err" role="alert">{{ batchError }}</p>
        <p v-else-if="!loadingBatches && extractionBatches.length === 0" class="text-xs text-faint">暂无可恢复批次。</p>
        <div v-else class="space-y-1.5 max-h-36 overflow-y-auto" aria-live="polite">
          <button
            v-for="batch in extractionBatches"
            :key="batch.id"
            type="button"
            class="w-full text-left p-2.5 rounded-lg border border-line hover:border-accent/40 bg-surface transition-colors"
            :class="batch.id === extractionId ? 'border-accent bg-accent/5' : ''"
            @click="resumeBatch(batch)"
          >
            <span class="flex items-center justify-between gap-2 text-xs">
              <span class="font-medium text-ink truncate">{{ formatBatchStatus(batch) }}</span>
              <span class="text-faint shrink-0">{{ formatBatchDate(batch.created_at) }}</span>
            </span>
            <span class="block mt-0.5 text-[11px] text-sub">
              {{ batch.candidates_count || 0 }} 个候选 · 待核对 {{ batch.pending_candidates || 0 }}
              <span v-if="batch.can_retry" class="text-warn"> · 可重试</span>
            </span>
            <span v-if="batch.recovery_reason" class="block mt-0.5 text-[11px] text-warn">{{ batch.recovery_reason }}</span>
          </button>
        </div>
        <div v-if="batchTotal > extractionBatches.length" class="flex justify-end gap-2">
          <button type="button" class="text-xs text-accent hover:underline" :disabled="loadingBatches" @click="loadMoreBatches">加载更多</button>
        </div>
      </section>

      <div v-if="loadingDocs" class="py-8 text-center text-faint text-sm">
        正在加载资料列表...
      </div>
      <div v-else-if="availableDocs.length === 0" class="py-8 text-center text-sub text-sm">
        暂无已解析完成的教材或补充资料，请先前往资料库上传。
      </div>
      <div v-else class="space-y-2 max-h-60 overflow-y-auto pr-1">
        <label
          v-for="doc in availableDocs"
          :key="doc.id"
          class="flex items-center justify-between p-3 rounded-xl border border-line hover:border-accent/40 bg-surface hover:bg-accent/10 cursor-pointer transition-all"
        >
          <div class="flex items-center gap-3">
            <input
              type="checkbox"
              :value="doc.id"
              v-model="selectedDocIds"
              class="w-4 h-4 text-accent rounded border-line-strong focus:ring-accent"
            >
            <div>
              <p class="text-sm font-medium text-ink">{{ doc.filename }}</p>
              <p class="text-xs text-faint">{{ doc.file_type === 'textbook' ? '参考教材' : '补充资料' }} · 共 {{ doc.page_count || 0 }} 页</p>
            </div>
          </div>
          <span class="text-xs text-faint font-mono">已解析</span>
        </label>
      </div>

      <p v-if="docError" class="text-xs text-err bg-err/10 p-2.5 rounded-lg border border-err/30">
        {{ docError }}
      </p>
    </div>

    <!-- Step 2: Processing -->
    <div v-else-if="step === 'processing'" class="py-8 text-center space-y-4">
      <div class="w-12 h-12 mx-auto rounded-full bg-accent/10 flex items-center justify-center text-accent">
        <svg class="w-6 h-6 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </div>
      <div>
        <h4 class="text-base font-semibold text-ink">正在分析资料并梳理考点</h4>
        <p class="text-xs text-sub mt-1 max-w-sm mx-auto">
          {{ taskStatusMessage || '正在按切片分批分析，提取核心概念与原文依据...' }}
        </p>
      </div>

      <!-- Progress Bar -->
      <div class="max-w-md mx-auto space-y-1">
        <div class="w-full bg-raised rounded-full h-2 overflow-hidden">
          <div
            class="bg-brand-600 h-2 rounded-full transition-all duration-300"
            :style="{ width: `${taskProgress}%` }"
          />
        </div>
        <div class="flex justify-between text-xs text-faint">
          <span>切片校验中</span>
          <span>{{ taskProgress }}%</span>
        </div>
      </div>
    </div>

    <!-- Step 3: Review Candidates -->
    <div v-else-if="step === 'review'" class="space-y-4">
      <div v-if="candidateLoadError" class="text-xs text-err bg-err/10 p-2.5 rounded-lg border border-err/30" role="alert">
        <p>{{ candidateLoadError }}</p>
        <button type="button" class="mt-1 text-accent hover:underline" :disabled="candidateLoading" @click="retryCandidates">
          {{ candidateLoading ? '正在重试...' : '只读重试加载候选' }}
        </button>
      </div>
      <p v-if="partialNotice" class="text-xs text-warn bg-warn/10 p-2.5 rounded-lg border border-warn/30">
        {{ partialNotice }}
      </p>
      <p v-if="hasUnresolvedSourceReview" class="text-xs text-warn bg-warn/10 p-2.5 rounded-lg border border-warn/30" role="status">
        仍有候选的来源需要核对；核对完成前不能提交本批审核。
      </p>
      <div v-if="legacyInvalidCandidates.length" class="text-xs text-warn bg-warn/10 p-2.5 rounded-lg border border-warn/30" role="status">
        <p>另有 {{ legacyInvalidCandidates.length }} 条历史来源失效候选，原审核决定无法确认，已保持只读；请在考点目录人工核对，不会自动重新入库。</p>
        <p class="mt-1 break-words">{{ legacyInvalidCandidates.map(candidate => candidate.name).join('、') }}</p>
      </div>
      <div class="flex items-center justify-between">
        <p class="text-xs text-sub">
          已从资料中提取出 <span class="font-bold text-ink">{{ candidates.length }}</span> 个考点候选。请逐项核对并确认：
        </p>
        <div v-if="candidates.length" class="flex items-center gap-2">
          <button
            type="button"
            class="text-xs text-accent hover:underline cursor-pointer"
            @click="setAllAction('accept')"
          >
            全部采纳
          </button>
          <span class="text-faint">|</span>
          <button
            type="button"
            class="text-xs text-sub hover:underline cursor-pointer"
            @click="setAllAction('ignore')"
          >
            全部忽略
          </button>
        </div>
      </div>

      <div class="space-y-3 max-h-[50vh] overflow-y-auto pr-1">
        <div
          v-for="cand in candidates"
          :key="cand.id"
          class="p-3.5 rounded-xl border transition-all text-sm"
          :class="getDecision(cand.id).action === 'ignore' ? 'border-line bg-raised opacity-60' : 'border-line bg-surface shadow-xs'"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1 space-y-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-semibold text-ink">{{ cand.name }}</span>
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
                  :class="getMatchBadgeClass(cand.match_type)"
                >
                  {{ getMatchBadgeLabel(cand.match_type) }}
                </span>
                <span v-if="cand.chapter" class="text-xs text-faint">
                  {{ cand.chapter }}
                </span>
              </div>
              <p v-if="cand.definition" class="text-xs text-sub leading-relaxed">
                {{ cand.definition }}
              </p>
            </div>

            <!-- Action Select -->
            <div class="flex items-center gap-1.5 shrink-0">
              <select
                v-model="getDecision(cand.id).action"
                class="text-xs rounded-lg border-line-strong py-1 px-2 focus:ring-accent"
              >
                <option value="accept">采纳入库</option>
                <option value="merge">合并至现有</option>
                <option value="ignore">忽略</option>
              </select>
            </div>
          </div>

          <!-- Merge target dropdown if merge is selected -->
          <div v-if="getDecision(cand.id).action === 'merge'" class="mt-2.5 pt-2 border-t border-line flex items-center gap-2">
            <span class="text-xs text-sub">选择合并目标:</span>
            <select
              v-model="getDecision(cand.id).target_kp_id"
              class="text-xs rounded-lg border-line-strong py-1 px-2 flex-1 focus:ring-accent"
            >
              <option value="">-- 请选择目标考点 --</option>
              <option v-for="kp in existingKPs" :key="kp.id" :value="kp.id">
                {{ kp.name }} ({{ kp.chapter || '未分类' }})
              </option>
            </select>
          </div>

          <!-- Citations View -->
              <div v-if="cand.citations && cand.citations.length > 0" class="mt-2.5 pt-2 border-t border-line">
            <details class="text-xs text-sub group">
              <summary class="cursor-pointer font-medium hover:text-accent flex items-center gap-1">
                <span>查看看出处依据 ({{ cand.citations.length }} 处)</span>
              </summary>
              <div class="mt-2 space-y-1.5 pl-2 border-l-2 border-line">
                <div v-for="(cit, cIdx) in cand.citations" :key="cIdx" class="text-xs">
                  <span class="font-medium text-ink">{{ cit.doc_name }}</span>
                  <span v-if="cit.page_number" class="text-faint ml-1">P{{ cit.page_number }}</span>
                  <p class="text-sub italic mt-0.5 bg-raised p-1.5 rounded">
                    “{{ cit.quote }}”
                  </p>
                </div>
              </div>

          <div v-if="cand.requires_source_review" class="mt-3 p-2.5 rounded-lg border border-warn/30 bg-warn/10">
            <div class="flex items-start justify-between gap-2">
              <p class="text-xs text-warn">该候选的旧依据需要重新核对后才能采纳。</p>
              <button type="button" class="btn btn-sm btn-secondary shrink-0" @click="openSourceReview(cand)">
                {{ sourceReviewCandidateId === cand.id ? '收起核对' : '核对依据' }}
              </button>
            </div>
            <p v-if="sourceReviewCandidateId !== cand.id && sourceReviewReasons[cand.id]" class="mt-1 text-[11px] text-sub">{{ sourceReviewReasons[cand.id] }}</p>
            <div v-if="sourceReviewCandidateId === cand.id" class="mt-2.5 space-y-2" role="group" :aria-label="`核对 ${cand.name} 的来源`">
              <p v-if="sourceReviewError" class="text-xs text-err" role="alert">{{ sourceReviewError }}</p>
              <button v-if="sourceReviewError" type="button" class="text-xs text-accent hover:underline" @click="openSourceReview(cand)">重新加载当前原文</button>
              <div v-if="sourceReviewLoading" class="text-xs text-faint">正在加载当前原文...</div>
              <template v-else-if="sourceReview">
                <label class="block text-xs text-sub">考点名称
                  <input v-model="sourceReviewForm.name" class="field mt-1 w-full" :disabled="sourceReviewSaving" />
                </label>
                <label class="block text-xs text-sub">章节
                  <input v-model="sourceReviewForm.chapter" class="field mt-1 w-full" :disabled="sourceReviewSaving" />
                </label>
                <div class="space-y-2">
                  <p class="text-xs font-medium text-ink">逐项确认当前原文</p>
                  <label v-for="(citation, idx) in sourceReview.citations" :key="`${citation.chunk_id}-${idx}`" class="block p-2 rounded-lg border border-line bg-surface text-xs">
                    <span class="flex items-start gap-2">
                      <input :checked="sourceReviewForm.citations[Number(idx)]?.confirmed" type="checkbox" class="mt-0.5" :disabled="sourceReviewSaving || !citation.content" @change="setSourceCitationConfirmed(Number(idx), ($event.target as HTMLInputElement).checked)" />
                      <span class="min-w-0"><span class="font-medium text-ink">{{ citation.doc_name }}<span v-if="citation.page_number"> · P{{ citation.page_number }}</span></span><span class="block mt-1 text-sub">{{ citation.content || citation.quote || '当前来源不可用' }}</span></span>
                    </span>
                  </label>
                </div>
                <p v-if="!sourceReview.can_confirm" class="text-xs text-err" role="alert">{{ sourceReview.reason || '当前来源不可确认' }}</p>
                <button type="button" class="btn btn-sm btn-primary" :disabled="sourceReviewSaving || !sourceReview.can_confirm || !allSourceCitationsConfirmed" @click="confirmSourceReview">
                  {{ sourceReviewSaving ? '保存核对...' : '确认当前依据' }}
                </button>
              </template>
            </div>
          </div>
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- Step 4: Suggest Question Links -->
    <div v-else-if="step === 'linking'" class="space-y-4">
      <p v-if="linkSaveError" class="text-xs text-err bg-err/10 p-2.5 rounded-lg border border-err/30" role="alert">{{ linkSaveError }}</p>
      <p v-if="linkSaveNotice" class="text-xs text-ok bg-ok/10 p-2.5 rounded-lg border border-ok/30" role="status">{{ linkSaveNotice }}</p>
      <div v-if="questionLinksData?.total_confirmed_questions === 0" class="py-6 text-center space-y-2">
        <div class="w-10 h-10 mx-auto rounded-full bg-raised flex items-center justify-center text-faint">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
        </div>
        <p class="text-sm font-semibold text-ink">当前题库暂无已确认真题</p>
        <p class="text-xs text-sub max-w-md mx-auto leading-relaxed">
          考点已成功入库。系统严格保证数据真实性，未凭空捏造虚假真题考查记录。您可随时在资料库导入真题后再进行关联。
        </p>
      </div>

      <div v-else-if="questionLinksData && questionLinksData.links.length === 0" class="py-6 text-center space-y-2">
        <p class="text-sm font-medium text-ink">未发现与新入库考点直接匹配的真题</p>
        <p class="text-xs text-faint">新考点已入库，后续可手动在题库中关联考点。</p>
      </div>

      <div v-else-if="questionLinksData && questionLinksData.links.length > 0" class="space-y-3">
        <p class="text-xs text-sub">
          检测到以下已确认真题与新考点高度相关，建议建立关联：
        </p>
        <div class="space-y-2 max-h-[50vh] overflow-y-auto pr-1">
          <label
            v-for="(link, lIdx) in questionLinksData.links"
            :key="lIdx"
            class="flex items-start gap-3 p-3 rounded-xl border border-line bg-surface hover:border-accent/30 cursor-pointer text-xs"
          >
            <input
              type="checkbox"
              v-model="selectedLinkIndexes"
              :value="lIdx"
              class="w-4 h-4 mt-0.5 text-accent rounded border-line-strong focus:ring-accent"
            >
            <div class="min-w-0 flex-1 space-y-1">
              <div class="flex items-center gap-2 font-medium text-ink">
                <span>{{ link.year ? `${link.year}年` : '' }} {{ link.session_name || '' }} {{ link.question_num || '' }}</span>
                <span class="inline-flex items-center gap-1 text-accent">关联至<ArrowRight class="h-3 w-3" />「{{ link.knowledge_point_name }}」</span>
              </div>
              <p class="text-sub line-clamp-2">
                {{ link.question_stem }}
              </p>
              <p v-if="link.reason" class="text-[11px] text-faint">
                匹配依据: {{ link.reason }}
              </p>
            </div>
          </label>
        </div>
      </div>
    </div>

    <!-- Step 5: Done — summary & next-step guidance -->
    <div v-else-if="step === 'done'" class="py-6 text-center space-y-4">
      <div class="w-12 h-12 mx-auto rounded-full bg-ok/15 flex items-center justify-center text-ok">
        <Check class="w-6 h-6" />
      </div>

      <div v-if="applyResult">
        <h4 class="text-base font-semibold text-ink">考点目录已更新</h4>
        <p class="text-xs text-sub mt-1 leading-relaxed">
          采纳 {{ applyResult.applied_count }} 个考点
          <template v-if="applyResult.merged_count > 0">，合并 {{ applyResult.merged_count }} 个</template>
          <template v-if="applyResult.ignored_count > 0">，忽略 {{ applyResult.ignored_count }} 个</template>
          <template v-if="linksAppliedCount > 0">；已建立 {{ linksAppliedCount }} 条真题关联</template>
        </p>
        <p v-if="linkLoadFailed" class="text-[11px] text-faint mt-1">
          真题关联建议加载失败，可稍后在题库中手动关联。
        </p>
      </div>
      <div v-else>
        <h4 class="text-base font-semibold text-ink">本次未识别到可入库的考点候选</h4>
        <p class="text-xs text-sub mt-1 max-w-sm mx-auto leading-relaxed">
          所选资料中未发现新的核心概念。可重新选择其他资料再次提取。
        </p>
      </div>

      <div class="pt-2 space-y-2.5">
        <p class="text-[11px] text-faint">下一步可以：</p>
        <div class="flex items-center justify-center gap-2 flex-wrap">
          <button
            v-if="!applyResult"
            type="button"
            class="btn btn-primary text-xs"
            @click="step = 'select'"
          >
            重新选择资料
          </button>
          <button
            v-else
            type="button"
            class="btn btn-primary text-xs"
            @click="finishAndClose"
          >
            查看考点目录
          </button>
          <template v-if="applyResult">
            <router-link to="/cards" class="btn btn-secondary text-xs" @click="finishAndClose">
              生成考点讲义
            </router-link>
            <router-link to="/today" class="btn btn-secondary text-xs" @click="finishAndClose">
              开始今日学习
            </router-link>
          </template>
        </div>
      </div>
    </div>

    <!-- Footer Actions -->
    <template #footer>
      <div class="flex items-center justify-between w-full">
        <button
          type="button"
          class="btn btn-secondary text-xs"
          :disabled="step === 'processing'"
          @click="handleSecondaryAction"
        >
          {{ step === 'linking' ? '暂不关联' : (step === 'done' ? '关闭' : '取消') }}
        </button>

        <div class="flex items-center gap-2">
          <button
            v-if="step === 'select'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="selectedDocIds.length === 0"
            @click="startExtraction"
          >
            开始梳理考点 (已选 {{ selectedDocIds.length }} 份)
          </button>

          <button
            v-else-if="step === 'review'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="isApplying || candidateLoading || !candidates.length || !!candidateLoadError || hasUnresolvedSourceReview"
            @click="applyCandidates"
          >
            {{ isApplying ? '正在入库...' : '确认并更新考点目录' }}
          </button>

          <button
            v-else-if="step === 'linking'"
            type="button"
            class="btn btn-primary text-xs"
            :disabled="isApplyingLinks"
            @click="confirmQuestionLinks"
          >
            {{ isApplyingLinks ? '正在保存...' : '完成考点梳理' }}
          </button>
        </div>
      </div>
    </template>
  </BaseModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import { ArrowRight, Check } from 'lucide-vue-next';
import { apiClient } from '../api/client';
import type { DocumentItem, ExtractionBatch, ExtractionCandidate, KnowledgePoint, ApplyCandidatesResponse, QuestionLinkSuggestionResponse } from '../types';
import BaseModal from './BaseModal.vue';

const props = defineProps<{
  show: boolean;
}>();

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'success'): void;
}>();

const step = ref<'select' | 'processing' | 'review' | 'linking' | 'done'>('select');
const availableDocs = ref<DocumentItem[]>([]);
const loadingDocs = ref(false);
const selectedDocIds = ref<string[]>([]);
const docError = ref<string | null>(null);

const extractionId = ref<string | null>(null);
const taskId = ref<string | null>(null);
const taskProgress = ref(0);
const taskStatusMessage = ref('');
let pollInterval: any = null;

const candidates = ref<ExtractionCandidate[]>([]);
const legacyInvalidCandidates = ref<ExtractionCandidate[]>([]);
const decisions = ref<Record<string, { action: 'accept' | 'ignore' | 'merge'; target_kp_id?: string; custom_name?: string }>>({});
const existingKPs = ref<KnowledgePoint[]>([]);
const isApplying = ref(false);

const questionLinksData = ref<QuestionLinkSuggestionResponse | null>(null);
const selectedLinkIndexes = ref<number[]>([]);
const isApplyingLinks = ref(false);

const pendingBatch = ref<ExtractionBatch | null>(null);
const extractionBatches = ref<ExtractionBatch[]>([]);
const batchTotal = ref(0);
const batchOffset = ref(0);
const loadingBatches = ref(false);
const batchError = ref('');
const candidateLoadError = ref('');
const candidateLoading = ref(false);
const partialNotice = ref('');
const hasApplied = ref(false);
const applyResult = ref<ApplyCandidatesResponse | null>(null);
const linksAppliedCount = ref(0);
const linkLoadFailed = ref(false);
const linkSaveError = ref('');
const linkSaveNotice = ref('');
const generation = ref(0);
const sourceReviewCandidateId = ref<string | null>(null);
const sourceReview = ref<any>(null);
const sourceReviewLoading = ref(false);
const sourceReviewSaving = ref(false);
const sourceReviewError = ref('');
const sourceReviewReasons = ref<Record<string, string>>({});
const sourceReviewForm = ref<{ name: string; chapter: string; citations: Array<any> }>({ name: '', chapter: '', citations: [] });
let successEmitted = false;

const client = apiClient as any;
const currentGeneration = () => generation.value;
const isCurrent = (token: number, batchId: string | null = extractionId.value) => token === generation.value && batchId === extractionId.value;
const batchStatusLabel: Record<string, string> = {
  processing: '进行中', pending: '等待中', completed: '待核对', partial: '部分完成', failed: '失败', cancelled: '已取消'
};
const formatBatchStatus = (batch: any) => batchStatusLabel[batch.status || batch.task_status] || '提取批次';
const formatBatchDate = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';

const isBatchRunning = (batch: ExtractionBatch | null) => {
  return !!batch && ['pending', 'processing', 'cancel_requested'].includes(batch.task_status || '');
};

const showResumeBanner = computed(() => {
  const b = pendingBatch.value;
  if (!b) return false;
  return isBatchRunning(b) || (b.pending_candidates ?? 0) > 0;
});

const loadInitialData = async () => {
  try {
    loadingDocs.value = true;
    docError.value = null;
    const docs = await apiClient.listMaterials();
    availableDocs.value = docs.filter(d => d.status === 'completed' && ['textbook', 'supplement'].includes(d.file_type));
    if (availableDocs.value.length > 0 && selectedDocIds.value.length === 0) {
      selectedDocIds.value = [availableDocs.value[0].id];
    }
    const kps = await apiClient.listKnowledgePoints();
    existingKPs.value = kps;
  } catch (err: any) {
    docError.value = err?.message || '加载资料失败';
  } finally {
    loadingDocs.value = false;
  }

  try {
    pendingBatch.value = await apiClient.getLatestExtraction();
  } catch {
    pendingBatch.value = null;
  }
  await loadBatches();
};

const loadBatches = async (append = false) => {
  if (loadingBatches.value) return;
  const token = currentGeneration();
  loadingBatches.value = true;
  batchError.value = '';
  const offset = append ? batchOffset.value : 0;
  try {
    if (typeof client.listExtractions !== 'function') {
      const latest = await apiClient.getLatestExtraction();
      if (token !== currentGeneration()) return;
      extractionBatches.value = latest ? [latest] : [];
      batchTotal.value = extractionBatches.value.length;
      batchOffset.value = extractionBatches.value.length;
      return;
    }
    const result = await client.listExtractions({ pending_only: false, limit: 20, offset });
    if (token !== currentGeneration()) return;
    const items = result?.items || [];
    extractionBatches.value = append ? [...extractionBatches.value, ...items] : items;
    batchTotal.value = Number(result?.total || extractionBatches.value.length);
    batchOffset.value = extractionBatches.value.length;
  } catch (err: any) {
    if (token === currentGeneration()) batchError.value = err?.response?.data?.detail || '加载提取批次失败';
  } finally {
    if (token === currentGeneration()) loadingBatches.value = false;
  }
};
const loadMoreBatches = () => loadBatches(true);

const resumePendingBatch = async () => {
  const batch = pendingBatch.value;
  if (!batch) return;
  await resumeBatch(batch);
};

const resumeBatch = async (batch: ExtractionBatch) => {
  stopPolling();
  generation.value += 1;
  pendingBatch.value = batch;
  extractionId.value = batch.id;
  taskId.value = batch.task_id || null;

  if (isBatchRunning(batch) && taskId.value) {
    step.value = 'processing';
    taskProgress.value = 10;
    taskStatusMessage.value = '正在等待上次提取任务完成...';
    startPolling();
  } else {
    await loadCandidates();
  }
};

const resetWizardState = () => {
  generation.value += 1;
  step.value = 'select';
  extractionId.value = null;
  taskId.value = null;
  taskProgress.value = 0;
  taskStatusMessage.value = '';
  candidates.value = [];
  legacyInvalidCandidates.value = [];
  decisions.value = {};
  questionLinksData.value = null;
  selectedLinkIndexes.value = [];
  pendingBatch.value = null;
  candidateLoadError.value = '';
  candidateLoading.value = false;
  partialNotice.value = '';
  hasApplied.value = false;
  applyResult.value = null;
  linksAppliedCount.value = 0;
  linkLoadFailed.value = false;
  linkSaveError.value = '';
  linkSaveNotice.value = '';
  sourceReviewCandidateId.value = null;
  sourceReview.value = null;
  sourceReviewError.value = '';
  sourceReviewReasons.value = {};
  successEmitted = false;
};

watch(() => props.show, (val) => {
  if (val) {
    resetWizardState();
    loadInitialData();
  } else {
    stopPolling();
  }
});

const startExtraction = async () => {
  if (selectedDocIds.value.length === 0) return;
  try {
    docError.value = null;
    step.value = 'processing';
    taskProgress.value = 5;
    taskStatusMessage.value = '正在创建提取任务...';

    const token = generation.value;
    const batch = await apiClient.createExtraction(selectedDocIds.value);
    if (token !== generation.value) return;
    extractionId.value = batch.id;
    taskId.value = batch.task_id;

    startPolling();
  } catch (err: any) {
    step.value = 'select';
    docError.value = err?.response?.data?.detail || err.message || '启动考点提取失败';
  }
};

const startPolling = () => {
  stopPolling();
  const token = generation.value;
  const batchId = extractionId.value;
  pollInterval = setInterval(async () => {
    if (!taskId.value || !extractionId.value || !isCurrent(token, batchId)) return;
    try {
      const task = await apiClient.getTaskStatus(taskId.value);
      taskProgress.value = task.progress;
      taskStatusMessage.value = task.message || '';

      if (!isCurrent(token, batchId)) return;
      if (task.status === 'completed' || task.status === 'partial') {
        stopPolling();
        if (task.status === 'partial') {
          partialNotice.value = task.message || '部分切片批次提取失败，以下为已成功识别的候选考点。';
        }
        await loadCandidates();
      } else if (task.status === 'failed' || task.status === 'cancelled') {
        stopPolling();
        step.value = 'select';
        docError.value = task.error_msg || task.message || '任务失败';
      }
    } catch (err) {
      console.error('Error polling extraction task:', err);
    }
  }, 1500);
};

const stopPolling = () => {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
};

const loadCandidates = async () => {
  if (!extractionId.value) return;
  const token = generation.value;
  const batchId = extractionId.value;
  candidateLoading.value = true;
  candidateLoadError.value = '';
  try {
    const cands = await apiClient.getExtractionCandidates(batchId);
    if (!isCurrent(token, batchId)) return;
    // Only pending candidates are actionable — already-decided ones must not be re-applied.
    const pending = cands.filter((c: ExtractionCandidate) => c.status === 'pending');
    candidates.value = pending;
    legacyInvalidCandidates.value = cands.filter((c: ExtractionCandidate) => c.status === 'invalid');

    if (pending.length === 0) {
      step.value = legacyInvalidCandidates.value.length ? 'review' : 'done';
      return;
    }

    // Initialize decisions
    const dMap: Record<string, any> = {};
    for (const c of pending) {
      dMap[c.id] = decisions.value[c.id] || {
          candidate_id: c.id,
          action: 'accept',
          target_kp_id: c.matched_kp_id || '',
          custom_name: c.name
        };
      if (c.requires_source_review && c.reason) sourceReviewReasons.value[c.id] = c.reason;
    }
    decisions.value = dMap;
    step.value = 'review';
  } catch (err: any) {
    if (!isCurrent(token, batchId)) return;
    candidateLoadError.value = err?.response?.data?.detail || '加载考点候选失败，当前恢复批次已保留。';
    // Keep the user in the recovered batch context. This retry must never
    // silently turn into a new extraction request.
    step.value = 'review';
  } finally {
    if (isCurrent(token, batchId)) candidateLoading.value = false;
  }
};

const retryCandidates = () => loadCandidates();

const getDecision = (candId: string) => {
  if (!decisions.value[candId]) {
    decisions.value[candId] = { action: 'accept', candidate_id: candId } as any;
  }
  return decisions.value[candId];
};

const setAllAction = (action: 'accept' | 'ignore') => {
  for (const c of candidates.value) {
    if (decisions.value[c.id]) {
      decisions.value[c.id].action = action;
    }
  }
};

const getMatchBadgeLabel = (type: string) => {
  switch (type) {
    case 'existing': return '现有有效考点';
    case 'deleted_or_ignored': return '曾删除/待核对';
    case 'source_changed': return '依据变更';
    default: return '新发现考点';
  }
};

const getMatchBadgeClass = (type: string) => {
  switch (type) {
    case 'existing': return 'bg-ok/15 text-ok';
    case 'deleted_or_ignored': return 'bg-warn/15 text-warn';
    default: return 'bg-accent/15 text-accent';
  }
};

const applyCandidates = async () => {
  if (!extractionId.value || !candidates.value.length || candidateLoadError.value) return;
  if (hasUnresolvedSourceReview.value) {
    candidateLoadError.value = '请先核对所有需要重新确认来源的候选。';
    return;
  }
  try {
    isApplying.value = true;
    const payload = Object.values(decisions.value).map(d => ({
      candidate_id: (d as any).candidate_id,
      version: candidates.value.find(c => c.id === (d as any).candidate_id)?.version ?? 0,
      action: d.action,
      target_kp_id: d.target_kp_id || undefined,
      custom_name: d.custom_name || undefined
    }));
    const token = generation.value;
    const batchId = extractionId.value;
    const res = await apiClient.applyCandidates(batchId, payload);
    if (!isCurrent(token, batchId)) return;
    applyResult.value = res;
    hasApplied.value = true;

    // Proceed to question linking step
    await loadQuestionLinks();
  } catch (err: any) {
    candidateLoadError.value = err?.response?.data?.detail || '保存考点决策失败，请检查后重试。';
  } finally {
    isApplying.value = false;
  }
};

const loadQuestionLinks = async () => {
  if (!extractionId.value) return;
  const token = generation.value;
  const batchId = extractionId.value;
  try {
    const data = await apiClient.getQuestionLinks(batchId);
    if (!isCurrent(token, batchId)) return;
    questionLinksData.value = data;
    selectedLinkIndexes.value = data.links.map((_: any, i: number) => i);
    step.value = 'linking';

  } catch (err) {
    console.error('Failed to load question links:', err);
    if (!isCurrent(token, batchId)) return;
    linkLoadFailed.value = true;
    linkSaveError.value = '真题关联建议加载失败，请重试或选择暂不关联。';
    step.value = 'linking';
  }
};

const confirmQuestionLinks = async () => {
  if (!extractionId.value || !questionLinksData.value) {
    step.value = 'done';
    return;
  }

  linkSaveError.value = '';
  linkSaveNotice.value = '';
  if (questionLinksData.value.links.length > 0 && selectedLinkIndexes.value.length > 0) {
    const token = generation.value;
    const batchId = extractionId.value;
    try {
      isApplyingLinks.value = true;
      const linksPayload = selectedLinkIndexes.value.map(idx => {
        const item = questionLinksData.value!.links[idx];
        return {
          question_id: item.question_id,
          knowledge_point_id: item.knowledge_point_id,
          action: 'accept'
        };
      });
      const res = await apiClient.applyQuestionLinks(batchId, linksPayload);
      if (!isCurrent(token, batchId)) return;
      linksAppliedCount.value = res?.applied_count ?? 0;
      const replayed = res?.already_linked_count ?? 0;
      linkSaveNotice.value = replayed ? `已保存 ${linksAppliedCount.value} 条关联，${replayed} 条已存在。` : '真题关联已保存。';
    } catch (err: any) {
      console.error('Failed to apply question links:', err);
      linkSaveError.value = err?.response?.data?.detail || '保存真题关联失败，请检查网络后重试。';
      return;
    } finally {
      isApplyingLinks.value = false;
    }
  }

  step.value = 'done';
};

const openSourceReview = async (candidate: ExtractionCandidate) => {
  if (sourceReviewCandidateId.value === candidate.id && !sourceReviewError.value) {
    sourceReviewCandidateId.value = null;
    return;
  }
  sourceReviewCandidateId.value = candidate.id;
  sourceReview.value = null;
  sourceReviewError.value = '';
  sourceReviewLoading.value = true;
  const token = generation.value;
  const batchId = extractionId.value;
  try {
    if (typeof client.getCandidateSourceReview !== 'function') throw new Error('当前版本暂不支持来源核对');
    const data = await client.getCandidateSourceReview(batchId, candidate.id);
    if (!isCurrent(token, batchId) || sourceReviewCandidateId.value !== candidate.id) return;
    sourceReview.value = data;
    sourceReviewForm.value = {
      name: data.name || candidate.name,
      chapter: data.chapter || candidate.chapter || '',
      citations: (data.citations || []).map((citation: any) => ({ ...citation, confirmed: false }))
    };
    if (!data.can_confirm && data.reason) sourceReviewReasons.value[candidate.id] = data.reason;
  } catch (err: any) {
    if (isCurrent(token, batchId)) sourceReviewError.value = err?.response?.data?.detail || err?.message || '加载来源核对信息失败';
  } finally {
    if (isCurrent(token, batchId)) sourceReviewLoading.value = false;
  }
};

const allSourceCitationsConfirmed = computed(() => sourceReviewForm.value.citations.length > 0 && sourceReviewForm.value.citations.every(c => c.confirmed));
const setSourceCitationConfirmed = (index: number, confirmed: boolean) => {
  const citation = sourceReviewForm.value.citations[index];
  if (citation) citation.confirmed = confirmed;
};

const confirmSourceReview = async () => {
  const candidateId = sourceReviewCandidateId.value;
  const batchId = extractionId.value;
  if (!candidateId || !batchId || !sourceReview.value || !allSourceCitationsConfirmed.value) return;
  sourceReviewSaving.value = true;
  sourceReviewError.value = '';
  const token = generation.value;
  try {
    if (typeof client.confirmCandidateSourceReview !== 'function') throw new Error('当前版本暂不支持来源核对');
    const payload = {
      version: sourceReview.value.version,
      name: sourceReviewForm.value.name,
      chapter: sourceReviewForm.value.chapter,
      citations: sourceReviewForm.value.citations.map(({ confirmed, ...citation }) => citation)
    };
    const updated = await client.confirmCandidateSourceReview(batchId, candidateId, payload);
    if (!isCurrent(token, batchId)) return;
    const index = candidates.value.findIndex(c => c.id === candidateId);
    if (index >= 0) candidates.value[index] = { ...candidates.value[index], ...(updated || {}), requires_source_review: false, source_status: 'valid', version: updated?.version ?? candidates.value[index].version } as any;
    sourceReviewCandidateId.value = null;
    sourceReview.value = null;
  } catch (err: any) {
    if (isCurrent(token, batchId)) sourceReviewError.value = err?.response?.data?.detail || err?.message || '来源已变化，请重新加载后核对';
  } finally {
    if (isCurrent(token, batchId)) sourceReviewSaving.value = false;
  }
};

const hasUnresolvedSourceReview = computed(() => candidates.value.some((candidate) => candidate.requires_source_review));

const emitSuccessOnce = () => {
  if (!successEmitted) {
    successEmitted = true;
    emit('success');
  }
};

const handleSecondaryAction = () => {
  if (step.value === 'linking') {
    // Candidates are already applied — land on the summary step instead of
    // closing silently so the user still gets next-step guidance.
    step.value = 'done';
  } else {
    handleClose();
  }
};

const finishAndClose = () => {
  emitSuccessOnce();
  handleClose();
};

const handleClose = () => {
  generation.value += 1;
  stopPolling();
  if (hasApplied.value) {
    emitSuccessOnce();
  }
  emit('close');
};

onMounted(() => {
  if (props.show) {
    loadInitialData();
  }
});

onUnmounted(stopPolling);
</script>
