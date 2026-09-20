import { onBeforeUnmount, shallowRef, watch, type Ref } from 'vue';
import type { FeedbackWidget } from '@feedback/web';
import { createFeedbackLogProvider, resolveFeedbackLogsApi } from '../utils/feedbackLogs';

/**
 * Custom Element 标签名，与 `@feedback/web` 导出的 `FEEDBACK_ELEMENT_TAG` 一致。
 *
 * 这里刻意不 import 那个常量：组件包必须保持动态 import（未配置、登录页、改密页都不加载它），
 * 顶层静态导入会把组件包直接拉进宿主产物，破坏懒加载。
 */
const FEEDBACK_ELEMENT_TAG = 'feedback-widget';

/**
 * 保留哨兵：**仅**用于绕过组件 `attributeChangedCallback` 的 `oldVal !== newVal` 守卫。
 *
 * 组件实现（0.4.0 源码核对，与 0.2.0 一致）：
 *   reflect(t, i) { i === null || i === "" ? this.removeAttribute(t) : this.setAttribute(t, i); }
 *   attributeChangedCallback(t, i, s) {
 *     t === "api-base" && i !== s && (this.identityEpoch++, this.resetForServiceSwitch()) ...
 *   }
 * 属性本来就不存在时，`removeAttribute` 不产生 DOM 变更 → 没有 custom element reaction
 * → 回调根本不触发 → 不会 reset。因此清理前若 `api-base` 缺失，先用本哨兵制造一次
 * 有效的 `null → 值` 变化（该变化本身即触发完整 reset），随后再置 null。
 *
 * 它**不是**有效服务基址，也绝不会被组件用于任何请求。
 */
const IDENTITY_RESET_SENTINEL = 'urn:rag:feedback:identity-reset';

/** 诊断前缀：本身不含任何用户信息，仅用于在控制台定位来源。 */
const DIAGNOSTIC_PREFIX = '[rag:feedback]';

/** 构建产物地址、跳转地址等 URL 一律不进诊断（含查询参数与令牌）。 */
const URL_IN_TEXT = /\bhttps?:\/\/[^\s"'`)]+/gi;
const MAX_DETAIL_LENGTH = 120;

/**
 * 脱敏：只保留错误类型与非敏感摘要。
 * - 摘要中的 URL（含查询参数）替换为 `<redacted-url>`；
 * - 非 Error 时不输出取值本身（取值可能是 URL 或令牌等）。
 * - 绝不输出用户名、路由、查询参数、令牌或 Cookie——本模块从头到尾没有读取过它们。
 */
function sanitizeDetail(error: unknown): string {
  if (error instanceof Error) {
    const name = typeof error.name === 'string' && error.name !== '' ? error.name : 'Error';
    const message = typeof error.message === 'string' ? error.message : '';
    const scrubbed = message.replace(URL_IN_TEXT, '<redacted-url>').replace(/\s+/g, ' ').trim();
    return scrubbed === '' ? name : `${name}: ${scrubbed.slice(0, MAX_DETAIL_LENGTH)}`;
  }
  return typeof error;
}

export interface UseFeedbackWidgetOptions {
  /** 挂载容器（App 根部的空 div）。容器出现/消失时自动挂载/卸载实例。 */
  container: Ref<HTMLElement | null>;
  /** 挂载门禁：会话初始化完成（auth.ready）且允许展示应用外壳。 */
  enabled: Ref<boolean>;
  /** 实例键 `userId:sessionEpoch`。键变化即重建实例，新实例绝不继承旧身份。 */
  instanceKey: Ref<string>;
  /** 页面标签：只取路由名称，绝不含 URL、查询参数或用户信息。 */
  pageLabel: Ref<string>;
  /** 实际主题（light/dark 已解析值）：只更新组件公开 theme 属性，不重建实例、不动草稿。 */
  theme?: Readonly<Ref<'light' | 'dark'>>;
}

/**
 * 反馈组件实例生命周期与身份隔离。
 *
 * - 只传路由名称作为 page-label；不传 URL、查询参数、用户信息或后台日志；
 * - 实例键 = `userId:sessionEpoch`，键变化时**先清空服务身份再移除旧实例**；
 * - 不重新实现组件已有的幂等键、状态轮询与草稿所有权（全部由组件内部负责）；
 * - 不向组件传递密码、Cookie 或令牌。
 */
export function useFeedbackWidget(options: UseFeedbackWidgetOptions) {
  // 只读构建期配置：客户端不持有 AI / Kaneo 密钥（归档目标由服务端登记）。
  const apiBase = (import.meta.env.VITE_FEEDBACK_API_BASE ?? '').trim();
  const appId = (import.meta.env.VITE_FEEDBACK_APP_ID ?? '').trim();
  /** 两个变量都必须填写；任一为空则既不加载组件包，也不挂载任何界面。 */
  const configured = apiBase !== '' && appId !== '';

  const element = shallowRef<FeedbackWidget | null>(null);
  let mountedKey: string | null = null;
  let moduleReady = false;
  let loadFailureReported = false;
  /** 竞态令牌：只有最后一次 sync 的结果才允许落到 DOM 上。 */
  let syncToken = 0;

  /** 诊断只含错误类型/摘要与 appId，绝不含用户名、URL、查询参数、令牌或 Cookie。 */
  const reportDiagnostic = (stage: string, error?: unknown) => {
    const detail = error === undefined ? '' : `（${sanitizeDetail(error)}）`;
    console.error(`${DIAGNOSTIC_PREFIX} ${stage}${detail}；appId=${appId}`);
  };

  /**
   * 只通过组件**公开的** `apiBase` 属性清空服务身份，触发其内部 `resetForServiceSwitch()`：
   * 取消在途捕获会话 / 登录握手 / 配额查询，停止轮询，清空访问令牌、任务态与草稿。
   *
   * 只 `remove()` 是不够的：组件的 `disconnectedCallback()` 不清理 `accessToken`、
   * `tokenExpiresAt`、`idempotencyKey`、`submitSnapshot`、`lastRecord` 与草稿字节。
   *
   * 返回自校验结果：`api-base` 属性必须确实已被移除。
   */
  const clearServiceIdentity = (el: FeedbackWidget): boolean => {
    if (el.getAttribute('api-base') === null) el.setAttribute('api-base', IDENTITY_RESET_SENTINEL);
    el.apiBase = null;
    return el.getAttribute('api-base') === null;
  };

  /**
   * 先清空服务身份，再移除实例。**无论自校验结果如何都无条件移除**：
   * 把状态不明的实例留在页面上，等于留下一个仍可点、且带旧独立登录态的反馈入口，
   * 比摘掉它更危险；移除后 `disconnectedCallback()` 会同步清掉定时器与监听器，
   * 宿主释放引用后实例不可达。
   */
  const destroyInstance = (reason: string) => {
    const el = element.value;
    element.value = null;
    mountedKey = null;
    if (!el) return;
    if (!clearServiceIdentity(el)) {
      // 只如实记录，不改变清理行为；机制是否真的生效由接入测试的探针判定失败。
      reportDiagnostic(`身份自校验未通过（${reason}），仍移除节点`);
    }
    el.remove();
  };

  const ensureModule = async (): Promise<boolean> => {
    if (moduleReady) return true;
    try {
      // 动态导入：未配置、登录页与强制改密页永远走不到这里，组件包不会被加载。
      await import('@feedback/web');
      moduleReady = true;
      loadFailureReported = false;
      return true;
    } catch (error) {
      if (!loadFailureReported) {
        loadFailureReported = true;
        reportDiagnostic('组件加载失败，本次不显示反馈入口（学习页面不受影响）', error);
      }
      return false;
    }
  };

  const createInstance = () => {
    const container = options.container.value;
    if (!container) return;
    const el = document.createElement(FEEDBACK_ELEMENT_TAG) as unknown as FeedbackWidget;
    // 先赋真实 api-base 再入 DOM：属性必定存在，清理时 removeAttribute 才会触发身份重置；
    // 同时保证元素不会以未配置状态出现在页面上。
    el.apiBase = apiBase;
    el.appId = appId;
    el.pageLabel = options.pageLabel.value;
    el.side = 'right';
    el.launcherMode = 'orb';
    // 使用组件既有的截图 / 预览 / 重拍 / 移除能力（含遮罩与失败保留草稿），不在此重新实现。
    el.captureMode = 'viewport';
    // theme 与 side/capture-mode 同属公开属性：赋值不触碰草稿、令牌与任务态（组件实现保证）。
    if (options.theme) el.theme = options.theme.value;
    // 桌面受限日志采集：仅当 preload 已注入受限通道且方法有效时才安装 logProvider；
    // 浏览器形态 / 旧 preload / 方法被篡改时保持 undefined，组件不采集、不报错。
    const feedbackLogsApi = resolveFeedbackLogsApi();
    if (feedbackLogsApi) {
      // isAlive 绑定**本实例**：destroyInstance 先把 element.value 置空再移除节点，
      // 此后旧 provider 迟到的结果一律作废为空数组，绝不写入后续实例的草稿。
      el.logProvider = createFeedbackLogProvider({
        api: feedbackLogsApi,
        isAlive: () => element.value === el && el.isConnected,
      });
    }
    container.appendChild(el);
    element.value = el;
  };

  const sync = async () => {
    const token = ++syncToken;
    if (!configured || !options.enabled.value) {
      destroyInstance(configured ? '会话未就绪或不允许展示外壳' : '未配置反馈服务');
      return;
    }
    const key = options.instanceKey.value;
    // 同一身份世代：不重建实例，草稿与截图保持不变。
    if (element.value && mountedKey === key) return;
    destroyInstance('用户身份或会话世代变化');
    if (token !== syncToken) return;
    if (!(await ensureModule())) return;
    if (token !== syncToken || !options.enabled.value) return;
    createInstance();
    mountedKey = key;
  };

  watch(
    [options.container, options.enabled, options.instanceKey],
    () => { void sync(); },
    { flush: 'post', immediate: true },
  );

  // 普通路由变化只更新 page-label（只传 route.name）：不重建实例、不清草稿。
  watch(
    options.pageLabel,
    (label) => { if (element.value) element.value.pageLabel = label; },
    { flush: 'post' },
  );

  // 主题变化只更新组件 theme 属性：不重建实例，草稿与截图保持不变。
  if (options.theme) {
    watch(
      options.theme,
      (next) => { if (element.value) element.value.theme = next; },
      { flush: 'post' },
    );
  }

  // 卸载前同样先清空服务身份，再移除实例。
  onBeforeUnmount(() => destroyInstance('宿主卸载'));

  return { configured };
}
