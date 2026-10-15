<template>
  <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
    <!-- Header -->
    <div class="mb-8">
      <h1 class="font-display text-2xl font-bold tracking-tight text-ink sm:text-3xl">模型与系统设置</h1>
      <p v-if="isDesktop" class="mt-2 text-sm text-sub">
        配置大语言模型、向量服务与百度 OCR 凭据。凭据通过系统本地安全存储加密保存，备份与安装包不会包含您的密钥。
      </p>
      <p v-else class="mt-2 text-sm text-sub">安卓和 Windows 网页共用服务端的模型与 OCR 配置，无需在每台设备重复保存。</p>
    </div>

    <!-- 外观：独立区域，选择立即生效，不随模型配置一起保存 -->
    <section class="surface mb-8 p-5" aria-labelledby="appearance-heading">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 id="appearance-heading" class="text-base font-semibold text-ink">外观</h2>
          <p class="mt-1 text-xs text-sub">跟随系统或固定浅色/深色，选择立即生效并保留在本设备上。</p>
          <p v-if="theme.persistError.value" role="alert" class="mt-2 text-xs text-err">{{ theme.persistError.value }}</p>
        </div>
        <div role="radiogroup" aria-label="外观" class="inline-flex rounded-xl border border-line bg-raised p-1">
          <button
            v-for="opt in themeOptions"
            :key="opt.value"
            type="button"
            role="radio"
            :aria-checked="theme.preference.value === opt.value"
            class="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
            :class="theme.preference.value === opt.value ? 'bg-surface text-accent shadow-xs' : 'text-sub hover:text-ink'"
            @click="chooseTheme(opt.value)"
          >
            <component :is="opt.icon" class="h-4 w-4" />{{ opt.label }}
          </button>
        </div>
      </div>
    </section>

    <!-- First-run Onboarding Wizard Banner -->
    <div
      v-if="isDesktop && showWizard"
      class="mb-8 rounded-xl border border-accent/30 bg-accent/10 p-5 shadow-xs"
    >
      <div class="flex items-start justify-between gap-4">
        <div>
          <h2 class="text-base font-semibold text-accent">首次使用配置向导</h2>
          <p class="mt-1 text-sm text-accent">
            配置模型凭据后即可开启专业课扫描真题文字识别、考点切片分析与智能练习批改。您也可以直接跳过此向导，本地阅读已导入的资料与已有背诵卡。
          </p>
        </div>
        <button
          type="button"
          class="shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium text-accent hover:bg-accent/15"
          @click="dismissWizard"
        >
          暂不配置，跳过向导
        </button>
      </div>
    </div>

    <!-- Error Banner -->
    <div v-if="errorMessage" class="mb-6 rounded-lg bg-err/10 p-4 text-sm text-err">
      {{ errorMessage }}
    </div>

    <!-- Software Updates -->
    <section v-if="isDesktop" class="surface mb-8 p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 class="text-base font-semibold text-ink">软件更新</h2>
          <p class="mt-1 text-xs text-sub">当前版本 {{ updateStatus.currentVersion }}，只从正式发行版获取更新。</p>
          <p v-if="updateStatus.state === 'available'" class="mt-2 text-sm text-accent">
            发现新版本 {{ updateStatus.version }}，确认后下载。
          </p>
          <p v-else-if="updateStatus.state === 'downloaded'" class="mt-2 text-sm text-ok">
            新版本 {{ updateStatus.version }} 已下载完成，可在空闲时安装。
          </p>
          <p v-else-if="updateStatus.state === 'downloading'" class="mt-2 text-sm text-accent">
            正在下载更新 {{ Math.round(updateStatus.percent || 0) }}%
          </p>
          <p v-else-if="updateStatus.state === 'error'" class="mt-2 text-sm text-err">{{ updateStatus.error }}</p>
          <p v-else-if="updateStatus.state === 'not-available'" class="mt-2 text-xs text-sub">当前已是最新版本。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            v-if="updateStatus.state !== 'downloading'"
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            :disabled="updateStatus.state === 'checking'"
            @click="handleCheckUpdates"
          >
            <RefreshCw class="h-3.5 w-3.5" />
            {{ updateStatus.state === 'checking' ? '检查中...' : '检查更新' }}
          </button>
          <button
            v-if="updateStatus.state === 'available'"
            type="button"
            class="btn btn-sm btn-primary gap-1.5"
            @click="handleDownloadUpdate"
          >
            <Download class="h-3.5 w-3.5" />下载更新
          </button>
          <button
            v-if="updateStatus.state === 'downloading'"
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            @click="handleCancelUpdate"
          >
            取消下载
          </button>
          <button
            v-if="updateStatus.state === 'downloaded'"
            type="button"
            class="btn btn-sm gap-1.5 bg-emerald-600 text-white shadow-sm hover:bg-emerald-700"
            @click="handleInstallUpdate"
          >
            <RefreshCw class="h-3.5 w-3.5" />重启安装
          </button>
        </div>
      </div>
    </section>

    <!-- Success Message -->
    <div v-if="successMessage" class="mb-6 rounded-lg bg-ok/10 p-4 text-sm text-ok">
      {{ successMessage }}
    </div>

    <section v-if="!isDesktop" class="space-y-5" aria-labelledby="server-config-heading" :aria-busy="loading">
      <div class="surface p-5">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 id="server-config-heading" class="text-base font-semibold text-ink">服务端统一配置</h2>
            <p class="mt-1 text-sm text-sub">此处显示当前服务端状态。修改地址、模型或密钥，请更新服务端配置并重启服务，再刷新本页。</p>
            <p class="mt-2 text-xs text-faint">“已配置”表示服务端已填写所需配置，不代表已验证连通性。刷新仅查询状态，不调用模型或 OCR。</p>
            <p class="mt-2 text-xs text-sub">点击测试将实际调用对应服务，可能产生少量费用。OCR 仅发送内置测试图片。</p>
          </div>
          <button type="button" class="btn btn-sm btn-secondary gap-1.5" :disabled="loading || serverTesting !== null" @click="refreshServerStatus">
            <RefreshCw class="h-3.5 w-3.5" :class="{ 'animate-spin': loading }" />
            {{ loading ? '读取中...' : '刷新状态' }}
          </button>
        </div>
      </div>
      <section v-for="service in serverServices" :key="service.id" class="surface p-5" :aria-busy="serverTesting === service.id">
        <div class="flex items-start justify-between gap-3 border-b border-line pb-4">
          <div class="flex items-center gap-2">
            <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent">
              <component :is="service.icon" class="h-4 w-4" />
            </div>
            <h3 class="text-base font-semibold text-ink">{{ service.name }}</h3>
          </div>
          <span class="shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium" :class="!service.available ? 'bg-raised text-sub' : service.configured ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn'">
            {{ !service.available ? (loading ? '读取中' : '未获取') : service.configured ? '已配置' : '待配置' }}
          </span>
        </div>
        <dl class="mt-4 grid gap-4 text-sm sm:grid-cols-2">
          <div v-if="service.isOcr" class="sm:col-span-2">
            <dt class="text-xs text-sub">当前识别方式</dt>
            <dd class="mt-1 break-all text-ink">{{ service.provider || '未获取' }}</dd>
          </div>
          <div :class="{ 'sm:col-span-2': !service.model }">
            <dt class="text-xs text-sub">{{ service.urlLabel }}</dt>
            <dd class="mt-1 break-all text-ink">{{ service.url || '未提供' }}</dd>
          </div>
          <div v-if="service.model">
            <dt class="text-xs text-sub">模型名称</dt>
            <dd class="mt-1 break-all text-ink">{{ service.model }}</dd>
          </div>
        </dl>
        <template v-if="service.isOcr">
          <p v-if="service.message && (!service.configured || (!serverTestResults[service.id] && serverTesting !== service.id))" class="mt-3 text-xs" :class="service.configured ? 'text-sub' : 'text-warn'">{{ service.message }}</p>
          <p class="mt-3 text-xs text-faint">支持 OpenAI 兼容视觉接口或百度 OCR，按服务端配置选择。</p>
        </template>
        <div class="mt-4 border-t border-line pt-4">
          <button
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            :aria-label="`${service.name}测试连通性`"
            :disabled="loading || serverTesting !== null || !service.available || !service.configured"
            @click="testServerService(service)"
          >
            <Loader2 v-if="serverTesting === service.id" class="h-3.5 w-3.5 animate-spin" />
            <RefreshCw v-else class="h-3.5 w-3.5" />
            {{ serverTesting === service.id ? '测试中...' : '测试连通性' }}
          </button>
          <div class="mt-3 text-sm" role="status" aria-live="polite">
            <p v-if="serverTesting === service.id" class="text-sub">正在使用服务端配置测试，请稍候…</p>
            <template v-else-if="serverTestResults[service.id]">
              <p class="font-medium" :class="serverTestResults[service.id]!.ok ? 'text-ok' : 'text-err'">
                {{ serverTestResults[service.id]!.ok ? '测试通过' : '测试未通过' }}
              </p>
              <p class="mt-1 break-words text-sub">{{ serverTestResults[service.id]!.message }}</p>
              <p class="mt-1 text-xs text-faint">
                耗时 {{ serverTestResults[service.id]!.duration }} 秒
                <span v-if="serverTestResults[service.id]!.callId" class="break-all"> · 诊断编号 {{ serverTestResults[service.id]!.callId }}</span>
              </p>
            </template>
          </div>
        </div>
      </section>
      <p v-if="status?.vector_locked" class="rounded-lg border border-warn/30 bg-warn/10 p-4 text-xs text-warn">
        向量索引已锁定：{{ status.vector_lock_reason || '模型来源不一致或信息缺失' }}。请联系服务端管理员核对配置与索引。
      </p>
    </section>

    <form v-if="isDesktop" @submit.prevent="handleSave" class="space-y-8">
      <!-- 1. LLM Settings -->
      <section class="surface p-6">
        <div class="flex items-center justify-between border-b border-line pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 text-accent">
              <Bot class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-ink">大语言模型 (LLM)</h2>
              <p class="text-xs text-sub">用于考点提炼、真题解析、背诵卡生成与主观题批改</p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.llm.configured ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn'"
          >
            {{ credentialsStatus?.llm.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label class="field-label">API 服务地址 (Base URL)</label>
            <input
              v-model="form.llm_base_url"
              type="text"
              placeholder="https://api.openai.com/v1"
              class="field mt-1"
            />
          </div>

          <div>
            <label class="field-label">模型名称 (Model)</label>
            <input
              v-model="form.llm_model"
              type="text"
              placeholder="gpt-4o-mini"
              class="field mt-1"
            />
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div class="sm:col-span-2" data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="field-label">API 密钥 (API Key)</label>
              <button
                v-if="credentialsStatus?.llm.configured"
                type="button"
                class="text-[11px] text-err hover:underline"
                @click="toggleClearLlmKey"
              >
                {{ clearLlmKey ? '撤销清除' : '清除已存密钥' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearLlmKey"
                v-model="form.llm_api_key"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.llm.configured ? '已保存密钥 (留空表示保持当前密钥不变)' : '请输入 API 密钥'"
                class="block w-full rounded-lg border border-line px-3 py-2 text-sm text-ink placeholder-faint focus:border-accent focus:outline-none"
              />
              <div v-else class="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-xs text-err">
                保存后将清除此模型的已存密钥。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            :disabled="testingLlm"
            @click="testLlm"
          >
            <Loader2 v-if="testingLlm" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-accent" />
            <span>{{ testingLlm ? '测试中...' : '测试 LLM 连通性' }}</span>
          </button>
          <span v-if="llmTestResult" class="text-right text-xs" :class="llmTestResult.ok ? 'text-ok' : 'text-err'">
            {{ llmTestResult.message }}
            <span v-if="!llmTestResult.ok && llmTestResult.callId" class="block text-faint" :title="llmTestResult.errorType">
              诊断编号 {{ llmTestResult.callId }}（可在日志页搜索）
            </span>
          </span>
        </div>
      </section>

      <!-- 2. Embedding Settings -->
      <section class="surface p-6">
        <div class="flex items-center justify-between border-b border-line pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-ok/10 text-ok">
              <Network class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-ink">向量检索服务 (Embedding)</h2>
              <p class="text-xs text-sub">用于学习资料语义切片检索与真题考点智能关联</p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.embedding.configured ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn'"
          >
            {{ credentialsStatus?.embedding.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <!-- Embedding lock note -->
        <div v-if="status?.vector_locked" class="mt-4 flex flex-col gap-2 rounded-lg border border-warn/40 bg-warn/10 p-4 text-xs text-warn">
          <div class="flex items-center gap-2 font-medium">
            <Lock class="h-4 w-4 text-warn" />
            <span>向量索引已被锁定：{{ status?.vector_lock_reason || '模型来源不一致或信息缺失' }}</span>
          </div>
          <p class="text-warn">
            锁定期间普通文本搜索与文档浏览不受影响，但禁止追加与语义检索。您可以在配置好上方向量模型与密钥后，点击下方按钮重建索引以解除锁定。
          </p>
          <div class="mt-1">
            <button
              type="button"
              class="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5 font-medium text-white shadow-xs hover:bg-amber-700 disabled:opacity-50"
              :disabled="loading || reindexing"
              @click="handleReindex"
            >
              <RefreshCw v-if="!reindexing" class="h-3.5 w-3.5" />
              <Loader2 v-else class="h-3.5 w-3.5 animate-spin" />
              <span>{{ reindexing ? '正在重建索引...' : '确认模型并重建索引' }}</span>
            </button>
          </div>
        </div>
        <div v-else-if="credentialsStatus?.embedding.locked" class="mt-4 rounded-lg border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
          <Lock class="mr-1.5 inline h-3.5 w-3.5 text-warn" />
          当前资料库已建立语义向量。为避免不同模型维度混用导致检索异常，已锁定服务地址与模型名称，仅允许更新密钥。
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label class="field-label">API 服务地址 (Base URL)</label>
            <input
              v-model="form.embedding_base_url"
              type="text"
              :disabled="credentialsStatus?.embedding.locked"
              placeholder="https://api.openai.com/v1"
              class="field mt-1 disabled:bg-raised disabled:text-faint"
            />
          </div>

          <div>
            <label class="field-label">模型名称 (Model)</label>
            <input
              v-model="form.embedding_model"
              type="text"
              :disabled="credentialsStatus?.embedding.locked"
              placeholder="text-embedding-3-small"
              class="field mt-1 disabled:bg-raised disabled:text-faint"
            />
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div class="sm:col-span-2" data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="field-label">API 密钥 (API Key)</label>
              <button
                v-if="credentialsStatus?.embedding.configured"
                type="button"
                class="text-[11px] text-err hover:underline"
                @click="toggleClearEmbeddingKey"
              >
                {{ clearEmbeddingKey ? '撤销清除' : '清除已存密钥' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearEmbeddingKey"
                v-model="form.embedding_api_key"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.embedding.configured ? '已保存密钥 (留空表示保持当前密钥不变)' : '请输入 API 密钥'"
                class="block w-full rounded-lg border border-line px-3 py-2 text-sm text-ink placeholder-faint focus:border-accent focus:outline-none"
              />
              <div v-else class="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-xs text-err">
                保存后将清除此向量服务的已存密钥。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            :disabled="testingEmbedding"
            @click="testEmbedding"
          >
            <Loader2 v-if="testingEmbedding" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-ok" />
            <span>{{ testingEmbedding ? '测试中...' : '测试 Embedding 连通性' }}</span>
          </button>
          <span v-if="embeddingTestResult" class="text-xs" :class="embeddingTestResult.ok ? 'text-ok' : 'text-err'">
            {{ embeddingTestResult.message }}
          </span>
        </div>
      </section>

      <!-- 3. Baidu PaddleOCR API Settings -->
      <section class="surface p-6">
        <div class="flex items-center justify-between border-b border-line pb-4">
          <div class="flex items-center space-x-2">
            <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-violet/10 text-violet">
              <ScanText class="h-4 w-4" />
            </div>
            <div>
              <h2 class="text-base font-semibold text-ink">百度 PP-OCRv6 API（异步 jobs 识别）</h2>
              <p class="text-xs text-sub">
                用于扫描版真题与图片讲义的高精度逐页文字识别：提交识别任务后轮询获取结果，单次识别可能需要数分钟
              </p>
            </div>
          </div>
          <span
            class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
            :class="credentialsStatus?.baidu_ocr.configured ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn'"
          >
            {{ credentialsStatus?.baidu_ocr.configured ? '已配置' : '待配置' }}
          </span>
        </div>

        <div class="mt-6 grid grid-cols-1 gap-4">
          <div>
            <label class="field-label">PP-OCRv6 jobs 接口地址</label>
            <input
              v-model="form.baidu_ocr_api_url"
              type="text"
              :placeholder="PP_OCR_V6_JOBS_URL"
              class="field mt-1"
            />
            <p class="mt-1 text-[11px] text-faint">
              请填写完整的 jobs 接口地址，例如 {{ PP_OCR_V6_JOBS_URL }}。
            </p>
            <p
              v-if="savedOcrUrlIsLegacy"
              data-ocr-legacy-hint
              class="mt-2 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] text-warn"
            >
              已保存的百度 OCR 地址不是 PP-OCRv6 jobs 地址，请更新为 PP-OCRv6 jobs 地址：{{
                PP_OCR_V6_JOBS_URL
              }}。在你点击“保存所有配置”之前，已保存的值不会被自动改写。
            </p>
          </div>

          <!-- Sensitive Input with Feedback Masking -->
          <div data-feedback-capture-mask>
            <div class="flex items-center justify-between">
              <label class="field-label">访问令牌 (Token)</label>
              <button
                v-if="credentialsStatus?.baidu_ocr.configured"
                type="button"
                class="text-[11px] text-err hover:underline"
                @click="toggleClearOcrToken"
              >
                {{ clearOcrToken ? '撤销清除' : '清除已存 Token' }}
              </button>
            </div>
            <div class="relative mt-1">
              <input
                v-if="!clearOcrToken"
                v-model="form.baidu_ocr_token"
                type="password"
                autocomplete="off"
                :placeholder="credentialsStatus?.baidu_ocr.configured ? '已保存 Token (留空表示保持当前 Token 不变)' : '请输入 Authorization Token'"
                class="block w-full rounded-lg border border-line px-3 py-2 text-sm text-ink placeholder-faint focus:border-accent focus:outline-none"
              />
              <div v-else class="rounded-lg border border-err/30 bg-err/10 px-3 py-2 text-xs text-err">
                保存后将清除此百度 OCR 的访问令牌。
              </div>
            </div>
          </div>
        </div>

        <div class="mt-4 flex items-center justify-between">
          <button
            type="button"
            class="btn btn-sm btn-secondary gap-1.5"
            :disabled="testingOcr"
            @click="testOcr"
          >
            <Loader2 v-if="testingOcr" class="h-3.5 w-3.5 animate-spin" />
            <Sparkles v-else class="h-3.5 w-3.5 text-violet" />
            <span>{{ testingOcr ? '测试中...' : '测试百度 OCR 识别能力' }}</span>
          </button>
          <span v-if="ocrTestResult" class="text-xs" :class="ocrTestResult.ok ? 'text-ok' : 'text-err'">
            {{ ocrTestResult.message }}
          </span>
        </div>
      </section>

      <!-- Save Button -->
      <div class="flex items-center justify-end gap-3 pt-2">
        <button
          type="submit"
          class="btn btn-primary gap-2 px-6"
          :disabled="loading"
        >
          <Loader2 v-if="loading" class="h-4 w-4 animate-spin" />
          <span>保存所有配置</span>
        </button>
      </div>
    </form>

    <!-- 4. Backup & Restore Section -->
    <div v-if="isDesktop" class="mt-12 border-t border-line pt-8">
      <div class="mb-4">
        <h2 class="text-lg font-bold text-ink">数据备份与恢复</h2>
        <p class="text-xs text-sub">
          通过系统文件对话框完整导出或恢复您的学习数据、题目与真题切片。
        </p>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <!-- Export Card -->
        <div class="surface p-5">
          <div class="flex items-center space-x-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-ok/10 text-ok">
              <Download class="h-5 w-5" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-ink">导出数据备份</h3>
              <p class="text-xs text-sub">校验哈希并生成压缩归档文件</p>
            </div>
          </div>
          <p class="mt-3 text-xs text-sub">服务安全停机后完整归档，自动排除系统日志与密钥敏感信息。</p>
          <button
            type="button"
            class="btn btn-secondary btn-sm mt-4 gap-1.5"
            :disabled="loading"
            @click="handleCreateBackup"
          >
            <Download class="h-3.5 w-3.5" />
            <span>选择保存路径并导出</span>
          </button>
        </div>

        <!-- Restore Card -->
        <div class="surface p-5">
          <div class="flex items-center space-x-3">
            <div class="flex h-9 w-9 items-center justify-center rounded-lg bg-warn/10 text-warn">
              <Upload class="h-5 w-5" />
            </div>
            <div>
              <h3 class="text-sm font-semibold text-ink">从备份文件恢复</h3>
              <p class="text-xs text-sub">安全恢复历史资料并保留当前快照</p>
            </div>
          </div>
          <p class="mt-3 text-xs text-sub">
            恢复前在临时目录自检完整性与模型兼容性，并自动保留恢复前副本。
          </p>
          <button
            type="button"
            class="btn btn-secondary btn-sm mt-4 gap-1.5 text-warn hover:bg-warn/10"
            :disabled="loading"
            @click="handleRestoreBackup"
          >
            <Upload class="h-3.5 w-3.5" />
            <span>选择备份文件并恢复</span>
          </button>
        </div>
      </div>

      <!-- Logs / Diagnostics -->
      <div class="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-raised p-4">
        <div class="text-xs text-sub">
          如遇异常需要排查，可在软件内直接查看运行日志并导出诊断包，或使用右下角 Feedback 提交报告。
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <!-- 设置页保留日志入口；主导航同时提供默认可见的「日志」标签页 -->
          <router-link
            v-if="isDesktop"
            to="/logs"
            class="btn btn-sm btn-secondary gap-1"
          >
            <ScrollText class="h-3.5 w-3.5 text-sub" />
            <span>查看运行日志</span>
          </router-link>
          <button
            type="button"
            class="btn btn-sm btn-secondary gap-1"
            @click="openLogDirectory"
          >
            <Folder class="h-3.5 w-3.5 text-sub" />
            <span>打开运行日志目录</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, type Component } from 'vue';
import {
  Bot,
  Network,
  ScanText,
  Lock,
  Sparkles,
  Download,
  Upload,
  Folder,
  Loader2,
  Monitor,
  Moon,
  RefreshCw,
  ScrollText,
  Sun,
} from 'lucide-vue-next';
import { useDesktop, PP_OCR_V6_JOBS_URL, isLegacyOcrApiUrl } from '../composables/useDesktop';
import { useTheme, type ThemePreference } from '../composables/useTheme';

const theme = useTheme();
const themeOptions: Array<{ value: ThemePreference; label: string; icon: Component }> = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
];
const chooseTheme = (value: ThemePreference) => {
  void theme.setPreference(value);
};

const {
  isDesktop,
  loading,
  status,
  credentialsStatus,
  errorMessage,
  fetchStatus,
  saveCredentials,
  testConnection,
  createBackup,
  restoreBackup,
  openLogDirectory,
  reindex,
  updateStatus,
  checkForUpdates,
  downloadUpdate,
  cancelUpdate,
  installUpdate,
} = useDesktop();

type ServerServiceId = 'llm' | 'embedding' | 'ocr';

interface ServerServiceCard {
  id: ServerServiceId;
  name: string;
  icon: Component;
  available: boolean;
  configured?: boolean;
  urlLabel: string;
  url?: string;
  model?: string;
  isOcr?: boolean;
  provider?: string;
  message?: string;
}

const serverServices = computed<ServerServiceCard[]>(() => [
  {
    id: 'llm',
    name: '大语言模型 (LLM)',
    icon: Bot,
    available: Boolean(credentialsStatus.value),
    configured: credentialsStatus.value?.llm.configured,
    urlLabel: 'API 服务地址',
    url: credentialsStatus.value?.llm.base_url,
    model: credentialsStatus.value?.llm.model,
  },
  {
    id: 'embedding',
    name: '向量检索服务 (Embedding)',
    icon: Network,
    available: Boolean(credentialsStatus.value),
    configured: credentialsStatus.value?.embedding.configured,
    urlLabel: 'API 服务地址',
    url: credentialsStatus.value?.embedding.base_url,
    model: credentialsStatus.value?.embedding.model,
  },
  {
    id: 'ocr',
    name: '文字识别 (OCR)',
    icon: ScanText,
    available: Boolean(credentialsStatus.value?.ocr),
    configured: credentialsStatus.value?.ocr?.configured,
    urlLabel: 'OCR 接口地址',
    url: credentialsStatus.value?.ocr?.base_url,
    model: credentialsStatus.value?.ocr?.model,
    isOcr: true,
    provider: credentialsStatus.value?.ocr?.provider,
    message: credentialsStatus.value?.ocr?.message || (!credentialsStatus.value?.ocr ? '未获取 OCR 配置状态，请确认服务端已更新后刷新。' : ''),
  },
]);

const serverTesting = ref<ServerServiceId | null>(null);
const serverTestResults = ref<Partial<Record<ServerServiceId, {
  ok: boolean;
  message: string;
  duration: string;
  callId?: string;
}>>>({});

const refreshServerStatus = async () => {
  if (serverTesting.value !== null || loading.value) return;
  serverTestResults.value = {};
  await fetchStatus();
};

const testServerService = async (service: ServerServiceCard) => {
  if (serverTesting.value !== null || loading.value || !service.available || !service.configured) return;
  serverTesting.value = service.id;
  delete serverTestResults.value[service.id];
  const started = performance.now();
  try {
    const res = await testConnection(service.id);
    serverTestResults.value[service.id] = {
      ok: res?.ok === true,
      message: res?.message || '服务未返回测试结果，请检查服务端日志。',
      duration: ((performance.now() - started) / 1000).toFixed(1),
      callId: res?.call_id,
    };
  } catch (err: any) {
    serverTestResults.value[service.id] = {
      ok: false,
      message: err?.message || '测试请求失败，请检查网络连接。',
      duration: ((performance.now() - started) / 1000).toFixed(1),
    };
  } finally {
    serverTesting.value = null;
  }
};

const reindexing = ref(false);

const handleCheckUpdates = async () => {
  await checkForUpdates();
};

const handleDownloadUpdate = async () => {
  await downloadUpdate();
};

const handleCancelUpdate = async () => {
  await cancelUpdate();
};

const handleInstallUpdate = async () => {
  if (!window.confirm('更新已下载。安装前软件会安全退出，安装完成后自动重新打开。现在安装吗？')) return;
  await installUpdate();
};

const handleReindex = async () => {
  const confirmed = window.confirm(
    '确认重建索引吗？\n\n系统将使用当前配置的向量模型重新计算所有切片的语义向量。这将消耗接口 Token 配额并产生少量费用。确认继续吗？'
  );
  if (!confirmed) return;

  reindexing.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const res = await reindex();
    if (res?.ok) {
      successMessage.value = res.message || '索引重建成功，向量锁已解除';
    } else {
      errorMessage.value = res?.message || '索引重建失败';
    }
  } catch (err: any) {
    errorMessage.value = err.message || '索引重建异常';
  } finally {
    reindexing.value = false;
  }
};

const showWizard = ref(false);
const successMessage = ref('');

const clearLlmKey = ref(false);
const clearEmbeddingKey = ref(false);
const clearOcrToken = ref(false);

const form = reactive({
  llm_base_url: '',
  llm_model: '',
  llm_api_key: '',
  embedding_base_url: '',
  embedding_model: '',
  embedding_api_key: '',
  baidu_ocr_api_url: '',
  baidu_ocr_token: '',
});

const testingLlm = ref(false);
const llmTestResult = ref<{ ok: boolean; message: string; callId?: string; errorType?: string } | null>(null);

const testingEmbedding = ref(false);
const embeddingTestResult = ref<{ ok: boolean; message: string } | null>(null);

const testingOcr = ref(false);
const ocrTestResult = ref<{ ok: boolean; message: string } | null>(null);

/**
 * 已保存的旧版百度 OCR 地址：只在页面上提示“请更新为 PP-OCRv6 jobs 地址”，
 * 绝不在加载时改写用户已保存的值 —— 只有用户显式点击保存才会写入。
 */
const savedOcrUrlIsLegacy = computed(() =>
  isLegacyOcrApiUrl(credentialsStatus.value?.baidu_ocr.api_url)
);

onMounted(async () => {
  await fetchStatus();
  if (!isDesktop.value) return;
  if (credentialsStatus.value) {
    form.llm_base_url = credentialsStatus.value.llm.base_url || 'https://api.openai.com/v1';
    form.llm_model = credentialsStatus.value.llm.model || 'gpt-4o-mini';
    form.embedding_base_url = credentialsStatus.value.embedding.base_url || 'https://api.openai.com/v1';
    form.embedding_model = credentialsStatus.value.embedding.model || 'text-embedding-3-small';
    // 新配置默认填入 PP-OCRv6 jobs 地址；已有保存值时原样显示，不做静默覆盖。
    form.baidu_ocr_api_url =
      credentialsStatus.value.baidu_ocr.api_url || PP_OCR_V6_JOBS_URL;

    // Show onboarding wizard if neither LLM nor OCR is configured
    const notConfigured =
      !credentialsStatus.value.llm.configured && !credentialsStatus.value.baidu_ocr.configured;
    const dismissed = localStorage.getItem('rag_wizard_dismissed');
    if (notConfigured && !dismissed) {
      showWizard.value = true;
    }
  }
});

const dismissWizard = () => {
  showWizard.value = false;
  localStorage.setItem('rag_wizard_dismissed', 'true');
};

const toggleClearLlmKey = () => {
  clearLlmKey.value = !clearLlmKey.value;
  if (clearLlmKey.value) form.llm_api_key = '';
};

const toggleClearEmbeddingKey = () => {
  clearEmbeddingKey.value = !clearEmbeddingKey.value;
  if (clearEmbeddingKey.value) form.embedding_api_key = '';
};

const toggleClearOcrToken = () => {
  clearOcrToken.value = !clearOcrToken.value;
  if (clearOcrToken.value) form.baidu_ocr_token = '';
};

const testLlm = async () => {
  testingLlm.value = true;
  llmTestResult.value = null;
  try {
    const res = await testConnection('llm', {
      base_url: form.llm_base_url,
      model: form.llm_model,
      api_key: form.llm_api_key || undefined,
    });
    llmTestResult.value = {
      ok: !!res?.ok,
      message: res?.message ?? 'LLM 测试未返回结果',
      callId: res?.call_id,
      errorType: res?.error_type,
    };
  } catch (err: any) {
    llmTestResult.value = { ok: false, message: err.message };
  } finally {
    testingLlm.value = false;
  }
};

const testEmbedding = async () => {
  testingEmbedding.value = true;
  embeddingTestResult.value = null;
  try {
    const res = await testConnection('embedding', {
      base_url: form.embedding_base_url,
      model: form.embedding_model,
      api_key: form.embedding_api_key || undefined,
    });
    embeddingTestResult.value = { ok: res.ok, message: res.message };
  } catch (err: any) {
    embeddingTestResult.value = { ok: false, message: err.message };
  } finally {
    testingEmbedding.value = false;
  }
};

const testOcr = async () => {
  testingOcr.value = true;
  ocrTestResult.value = null;
  try {
    const res = await testConnection('baidu_ocr', {
      baidu_ocr_url: form.baidu_ocr_api_url,
      baidu_ocr_token: form.baidu_ocr_token || undefined,
    });
    ocrTestResult.value = { ok: res.ok, message: res.message };
  } catch (err: any) {
    ocrTestResult.value = { ok: false, message: err.message };
  } finally {
    testingOcr.value = false;
  }
};

const handleSave = async () => {
  successMessage.value = '';
  try {
    const payload: any = {
      llm_base_url: form.llm_base_url,
      llm_model: form.llm_model,
      embedding_base_url: form.embedding_base_url,
      embedding_model: form.embedding_model,
      baidu_ocr_api_url: form.baidu_ocr_api_url,
    };

    if (clearLlmKey.value) {
      payload.llm_api_key = { action: 'clear' };
    } else if (form.llm_api_key) {
      payload.llm_api_key = { action: 'replace', value: form.llm_api_key };
    } else {
      payload.llm_api_key = { action: 'keep' };
    }

    if (clearEmbeddingKey.value) {
      payload.embedding_api_key = { action: 'clear' };
    } else if (form.embedding_api_key) {
      payload.embedding_api_key = { action: 'replace', value: form.embedding_api_key };
    } else {
      payload.embedding_api_key = { action: 'keep' };
    }

    if (clearOcrToken.value) {
      payload.baidu_ocr_token = { action: 'clear' };
    } else if (form.baidu_ocr_token) {
      payload.baidu_ocr_token = { action: 'replace', value: form.baidu_ocr_token };
    } else {
      payload.baidu_ocr_token = { action: 'keep' };
    }

    const res = await saveCredentials(payload);
    successMessage.value = res.message || '配置已成功安全保存';

    // Clear sensitive inputs from UI memory
    form.llm_api_key = '';
    form.embedding_api_key = '';
    form.baidu_ocr_token = '';
    clearLlmKey.value = false;
    clearEmbeddingKey.value = false;
    clearOcrToken.value = false;
  } catch {
    // errorMessage is set by composable
  }
};

const handleCreateBackup = async () => {
  try {
    const res = await createBackup();
    if (res && res.ok) {
      alert(`备份导出成功！\n文件路径: ${res.archive_path}`);
    }
  } catch (err: any) {
    alert(`备份导出失败: ${err.message}`);
  }
};

const handleRestoreBackup = async () => {
  if (!confirm('恢复备份将替换当前数据并重新加载，恢复前会自动保留历史副本。是否继续？')) return;
  try {
    const res = await restoreBackup();
    if (res && res.ok) {
      alert('数据恢复成功！');
    }
  } catch (err: any) {
    alert(`恢复失败: ${err.message}`);
  }
};
</script>
