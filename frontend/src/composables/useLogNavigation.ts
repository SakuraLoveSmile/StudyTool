/**
 * 外壳级"打开运行日志"导航状态与诊断（T2a）。
 *
 * 为什么必须独立于 LogsView：`/logs` 是懒加载路由，chunk 加载失败时页面组件根本
 * 渲染不出来，因此 pending / slow / failed 状态与提示 UI 只能挂在外壳层
 * （Navbar 与 main 之间），不能写进 LogsView。
 *
 * 接线（全部为可选、幂等）：
 *   router/index.ts   → installLogNavigation(router)（已在路由模块内调用；
 *                     主代理在 main.ts 再调一次也安全）
 *   Navbar.vue        → 日志入口 @click="beginLogNavigation('click')"
 *   App.vue（主代理） → <LogNavNotice v-if="showShell" /> 放在 <Navbar/> 之后
 *   LogsView.vue      → markLogPageMounted() + reportLogQuery*(...) 埋点
 *
 * 诊断编号：每次"打开日志页"意图生成 nav-<ts36>-<seq>，贯穿点击、守卫放行、
 * 挂载、首次查询各事件 fields.nav_id，现场导出后可按编号串起整条链路。
 *
 * 脱敏约定：只记路由 name、阶段、错误码与去 URL-origin 的错误摘要；
 * 不记 fullPath / query / Cookie / 令牌 / 用户输入。
 */

import { reactive, readonly } from 'vue';
import {
  isNavigationFailure,
  NavigationFailureType,
  type Router,
} from 'vue-router';
import { flushReports, report } from '../utils/diagnostics';

export type LogNavPhase = 'idle' | 'pending' | 'slow' | 'failed';
export type LogNavTrigger = 'click' | 'route' | 'retry';

/** 超过该时长未完成即提示"打开时间较长"（不强制刷新、不清草稿）。 */
const SLOW_HINT_MS = 10_000;
const MAX_MESSAGE_LENGTH = 160;

interface LogNavState {
  phase: LogNavPhase;
  navId: string | null;
  trigger: LogNavTrigger | null;
  /** 展示给用户的脱敏错误摘要（已剥离 URL origin）。 */
  errorSummary: string;
  startedAt: number;
}

const state = reactive<LogNavState>({
  phase: 'idle',
  navId: null,
  trigger: null,
  errorSummary: '',
  startedAt: 0,
});

let installed = false;
let routerRef: Router | null = null;
let navSeq = 0;
let slowTimer: ReturnType<typeof setTimeout> | null = null;
let mountedReportedFor: string | null = null;
let queryStartReportedFor: string | null = null;
let firstQueryDoneFor: string | null = null;

/** 生成一次"打开日志页"意图的诊断编号。 */
function newNavId(): string {
  navSeq += 1;
  return `nav-${Date.now().toString(36)}-${navSeq.toString(36)}`;
}

/**
 * 剥离错误消息里的 URL origin，只保留路径部分（chunk 文件名有定位价值，
 * 本身不含用户输入）；其余文本原样截断。
 */
function sanitizeErrorMessage(value: unknown): string {
  let text = '';
  try {
    if (value instanceof Error) text = String(value.message ?? value.name ?? '');
    else text = String(value ?? '');
  } catch {
    return '';
  }
  return text
    .replace(/[a-z][a-z0-9+.-]*:\/\/[^\s'")]+/gi, (m) => {
      try {
        return new URL(m).pathname;
      } catch {
        return '[url]';
      }
    })
    .slice(0, MAX_MESSAGE_LENGTH);
}

function clearSlowTimer(): void {
  if (slowTimer !== null) {
    clearTimeout(slowTimer);
    slowTimer = null;
  }
}

/** pending 计时：超时只把状态推进到 slow 并记录一次 WARN，不做任何强制动作。 */
function armSlowTimer(): void {
  clearSlowTimer();
  slowTimer = setTimeout(() => {
    slowTimer = null;
    if (state.phase !== 'pending') return;
    state.phase = 'slow';
    report('WARN', 'log_nav.slow', '日志页打开超过 10 秒仍未完成', {
      nav_id: state.navId,
      elapsed_ms: Date.now() - state.startedAt,
    });
    void flushReports();
  }, SLOW_HINT_MS);
}

/** 终结当前导航状态（成功 / 被取代 / 用户去了别的页面）。 */
function resetState(): void {
  clearSlowTimer();
  state.phase = 'idle';
  state.errorSummary = '';
  state.startedAt = 0;
  // navId 保留最后一次，便于 LogsView 挂载后仍能关联查询埋点。
}

/**
 * 开始（或复用）一次日志导航。
 * - 'click'：Navbar 入口点击时调用，立即显示"正在打开"；
 * - 'route'：守卫兜底（地址栏直达 / 程序内 push），导航已开始但未见过 click；
 * - 'retry'：提示条上的重试，沿用同一 navId 以关联同一诊断链。
 */
export function beginLogNavigation(trigger: LogNavTrigger): string {
  // 同一意图进行中（点击→守卫连续触发）直接复用编号。
  if (state.phase === 'pending' && state.navId) return state.navId;
  if (trigger === 'retry' && state.navId) {
    state.phase = 'pending';
    state.trigger = 'retry';
    state.errorSummary = '';
    state.startedAt = Date.now();
    armSlowTimer();
    return state.navId;
  }
  state.navId = newNavId();
  state.trigger = trigger;
  state.phase = 'pending';
  state.errorSummary = '';
  state.startedAt = Date.now();
  mountedReportedFor = null;
  queryStartReportedFor = null;
  firstQueryDoneFor = null;
  armSlowTimer();
  if (trigger === 'click') {
    report('INFO', 'log_nav.click', '用户点击日志入口', { nav_id: state.navId });
  }
  return state.navId;
}

function failLogNavigation(error: unknown, code: string): void {
  // 只有目标确为日志页的进行中导航才更新外壳；旧导航错误不得覆盖新状态。
  if (!state.navId) return;
  clearSlowTimer();
  const summary = sanitizeErrorMessage(error) || code;
  state.phase = 'failed';
  state.errorSummary = summary;
  report('ERROR', 'log_nav.failed', '打开日志页失败', {
    nav_id: state.navId,
    code,
    trigger: state.trigger,
    elapsed_ms: Date.now() - state.startedAt,
    error_type: error instanceof Error ? error.name : typeof error,
    error_message: sanitizeErrorMessage(error),
  });
  // 失败证据必须尽快落盘：用户可能马上关窗口，不等 1s 聚合队列。
  void flushReports();
}

function succeedLogNavigation(): void {
  if (state.phase === 'idle') return;
  report('INFO', 'log_nav.ok', '日志页导航完成', {
    nav_id: state.navId,
    elapsed_ms: Date.now() - state.startedAt,
  });
  resetState();
}

/**
 * 安装路由级日志导航处理：幂等，重复调用安全。
 * 只挂监听、不拦截导航（守卫不返回值），普通页面导航行为完全不变。
 */
export function installLogNavigation(router: Router): void {
  if (installed) return;
  installed = true;
  routerRef = router;

  router.beforeEach((to) => {
    // 兜底：地址栏直达或程序内 push 到 /logs，没有 click 事件也要起一条链。
    if (to.name === 'logs') {
      const navId = beginLogNavigation('route');
      report('INFO', 'log_nav.start', '日志页导航开始', {
        nav_id: navId,
        trigger: state.trigger,
      });
    }
    // 不返回值 = 放行；守卫抛错会走 onError。
  });

  router.onError((error, to) => {
    // 通用上报：任何导航失败 / 动态 chunk 加载失败都留痕（只记 name 与脱敏摘要）。
    report('ERROR', 'router.error', '路由导航发生错误', {
      to_name: String(to?.name ?? 'unknown'),
      error_type: error instanceof Error ? error.name : typeof error,
      error_message: sanitizeErrorMessage(error),
    });
    if (to?.name === 'logs') {
      failLogNavigation(error, 'ROUTE_ERROR');
    }
    void flushReports();
  });

  router.afterEach((to, _from, failure) => {
    if (failure) {
      /**
       * cancelled / duplicated / 重定向：一定有取代者导航会走自己的完整链路，
       * 这里绝不能 reset——它的 afterEach 可能晚于新导航的 beforeEach，
       * reset 会误清新导航刚建立的 pending 状态。
       * aborted（守卫返回 false）：本次导航真正死亡且无后继，
       * 若目标是日志页且仍 pending/slow，记一笔后终结。
       */
      if (
        isNavigationFailure(failure, NavigationFailureType.aborted) &&
        to?.name === 'logs' &&
        (state.phase === 'pending' || state.phase === 'slow')
      ) {
        report('WARN', 'log_nav.aborted', '日志页导航被守卫中止', {
          nav_id: state.navId,
        });
        resetState();
      } else if (
        // duplicated：已在 /logs 再点入口，没有后继导航会替它收尾——
        // 语义上已到达目的地，直接终结，否则 pending 会常驻并误报"打开时间较长"。
        isNavigationFailure(failure, NavigationFailureType.duplicated) &&
        to?.name === 'logs' &&
        state.phase !== 'idle'
      ) {
        resetState();
      }
      return;
    }
    // 无 failure：导航真正落定。
    if (to.name === 'logs') {
      succeedLogNavigation();
    } else if (state.phase !== 'idle') {
      // 用户已去其他页面：旧 pending/slow/failed 不得残留覆盖新页面。
      resetState();
    }
  });
}

/** 提示条"重试"：沿用同一 navId 走正常 router.push，不加随机参数、不自动连发。 */
export function retryLogNavigation(): void {
  const router = routerRef;
  const navId = state.navId;
  report('INFO', 'log_nav.retry', '用户重试打开日志页', { nav_id: navId });
  beginLogNavigation('retry');
  if (!router) return;
  // push 的 rejection 已由 router.onError 统一捕获，这里吞掉避免 unhandledrejection 噪音。
  void router.push({ name: 'logs' }).catch(() => {
    /* handled by router.onError */
  });
}

/** 用户手动关闭提示条。 */
export function dismissLogNavigation(): void {
  resetState();
}

/** LogsView 挂载后调用：同一 navId 只记一次。 */
export function markLogPageMounted(): void {
  if (!state.navId || mountedReportedFor === state.navId) return;
  mountedReportedFor = state.navId;
  report('INFO', 'log_nav.mounted', '日志页组件已挂载', { nav_id: state.navId });
}

/** 当前诊断编号（LogsView 首次查询埋点关联用）。 */
export function currentLogNavId(): string | null {
  return state.navId;
}

/**
 * 首次查询埋点：同一 navId 的 start/ok/failed 各只记一次；
 * 取消/被取代属正常控制流，不计为失败。自动刷新一轮不触发（LogsView 只在首次调用）。
 */
export function reportLogQueryStart(): void {
  if (!state.navId || queryStartReportedFor === state.navId) return;
  queryStartReportedFor = state.navId;
  report('INFO', 'log_query.start', '日志页首次查询开始', { nav_id: state.navId });
}

export function reportLogQueryOk(elapsedMs?: number): void {
  if (!state.navId || firstQueryDoneFor === state.navId) return;
  firstQueryDoneFor = state.navId;
  report('INFO', 'log_query.ok', '日志页首次查询成功', {
    nav_id: state.navId,
    elapsed_ms: elapsedMs,
  });
}

export function reportLogQueryFailed(code?: string, message?: string): void {
  if (!state.navId || firstQueryDoneFor === state.navId) return;
  firstQueryDoneFor = state.navId;
  report('ERROR', 'log_query.failed', '日志页首次查询失败', {
    nav_id: state.navId,
    code: code || 'UNKNOWN',
    error_message: (message || '').slice(0, MAX_MESSAGE_LENGTH),
  });
  void flushReports();
}

/** 只读状态视图：LogNavNotice 订阅它渲染。 */
export const logNavState = readonly(state);
