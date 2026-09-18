/**
 * 模拟 Feedback 服务（**仅用于测试**，绝不向真实服务提交）。
 *
 * 用 Playwright 的 `page.route` 拦截到 `VITE_FEEDBACK_API_BASE` 的请求，
 * 因此**不需要**真的监听端口；但因为是跨 origin 请求（页面在 127.0.0.1:5274，
 * 反馈服务在 127.0.0.1:5199），必须自行补齐 CORS 响应头。
 */

export interface RecordedRequest {
  method: string;
  pathname: string;
  url: string;
  headers: Record<string, string>;
  bodyText: string;
  at: number;
}

/** 受控 Promise：可先拿住请求、稍后（切换身份之后）再放行响应。 */
export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
  settled: boolean;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const d: Deferred<T> = {
    promise: new Promise<T>((res, rej) => { resolve = res; reject = rej; }),
    resolve: (value) => { d.settled = true; resolve(value); },
    reject: (reason) => { d.settled = true; reject(reason); },
    settled: false,
  };
  return d;
}

export interface MockResponse {
  status?: number;
  body?: unknown;
  /** 原始响应体（与 body 二选一）；用于模拟非 JSON 响应。 */
  rawBody?: string;
  contentType?: string;
  headers?: Record<string, string>;
}

export interface HandlerContext {
  request: RecordedRequest;
  index: number;
}

export type Handler = MockResponse | ((ctx: HandlerContext) => MockResponse | Promise<MockResponse>);

export interface RouteRule {
  method: string;
  /** 路径匹配：正则或字符串（字符串为精确匹配 pathname）。 */
  path: string | RegExp;
  /** 每次命中依次取用；用尽后复用最后一条。 */
  handler: Handler | Handler[];
}

export interface MockService {
  requests: RecordedRequest[];
  /** 某个 method 的请求数（不传 method 表示全部）。 */
  count(method?: string): number;
  countOf(method: string, path: string | RegExp): number;
  /** 按顺序匹配到的请求。 */
  all(method: string, path: string | RegExp): RecordedRequest[];
  clear(): void;
}

const DEFAULT_LOGIN_BODY = () => ({
  ok: true,
  token: 'mock-token-0001',
  expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  user: { id: 'fb-user-1', username: 'feedback-user', role: 'user' as const },
  quota: {
    dailyLimit: 20,
    used: 0,
    remaining: 20,
    resetAt: new Date(Date.now() + 3600_000).toISOString(),
  },
});

function matches(rule: RouteRule, method: string, pathname: string): boolean {
  if (rule.method.toUpperCase() !== method.toUpperCase()) return false;
  return typeof rule.path === 'string' ? rule.path === pathname : rule.path.test(pathname);
}

/**
 * 把一组规则装到 page 上。命中顺序 = 最后注册的规则优先（Playwright route 语义）。
 * 因此调用方应把「基础放行」规则与「受控挂起」规则分开注册。
 */
export function installMockService(
  page: import('@playwright/test').Page,
  baseUrl: string,
  rules: RouteRule[],
): MockService {
  const requests: RecordedRequest[] = [];
  const hitCount = new Map<RouteRule, number>();

  const service: MockService = {
    requests,
    count: (method) => (method ? requests.filter((r) => r.method.toUpperCase() === method.toUpperCase()).length : requests.length),
    countOf: (method, path) => service.all(method, path).length,
    all: (method, path) => requests.filter((r) => {
      if (r.method.toUpperCase() !== method.toUpperCase()) return false;
      return typeof path === 'string' ? r.pathname === path : path.test(r.pathname);
    }),
    clear: () => { requests.length = 0; },
  };

  const originPattern = new RegExp(`^${baseUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(/|$)`);

  void page.route(originPattern, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method().toUpperCase();

    // CORS 预检：不记录为业务请求，直接放行。
    if (method === 'OPTIONS') {
      await route.fulfill({
        status: 204,
        headers: {
          'access-control-allow-origin': request.headers()['origin'] ?? '*',
          'access-control-allow-methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
          'access-control-allow-headers': request.headers()['access-control-request-headers'] ?? '*',
          'access-control-max-age': '600',
        },
      });
      return;
    }

    const recorded: RecordedRequest = {
      method,
      pathname: url.pathname,
      url: request.url(),
      headers: request.headers(),
      bodyText: request.postData() ?? '',
      at: Date.now(),
    };
    requests.push(recorded);

    const rule = [...rules].reverse().find((r) => matches(r, method, url.pathname));
    if (!rule) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        headers: corsHeaders(request.headers()['origin']),
        body: JSON.stringify({ error: { code: 'not_found', message: `无规则匹配 ${method} ${url.pathname}` } }),
      });
      return;
    }

    const index = hitCount.get(rule) ?? 0;
    hitCount.set(rule, index + 1);
    const queue = Array.isArray(rule.handler) ? rule.handler : [rule.handler];
    const handler = queue[Math.min(index, queue.length - 1)];
    const response = typeof handler === 'function' ? await handler({ request: recorded, index }) : handler;

    await route.fulfill({
      status: response.status ?? 200,
      headers: { ...corsHeaders(request.headers()['origin']), ...(response.headers ?? {}) },
      contentType: response.contentType ?? 'application/json',
      body: response.rawBody ?? JSON.stringify(response.body ?? {}),
    });
  });

  return service;
}

function corsHeaders(origin: string | undefined): Record<string, string> {
  return {
    'access-control-allow-origin': origin ?? '*',
    'access-control-allow-headers': 'authorization,content-type,idempotency-key',
    'access-control-expose-headers': '*',
  };
}

/** 常用规则构造。 */
export const feedbackRules = {
  /** 登录成功（返回固定令牌）。 */
  loginOk(overrides: Partial<ReturnType<typeof DEFAULT_LOGIN_BODY>> = {}): RouteRule {
    return {
      method: 'POST',
      path: '/api/auth/login',
      handler: { status: 200, body: { ...DEFAULT_LOGIN_BODY(), ...overrides } },
    };
  },
  /** 登录被拒（凭据错误）。 */
  loginUnauthorized(): RouteRule {
    return {
      method: 'POST',
      path: '/api/auth/login',
      handler: { status: 401, body: { error: { code: 'invalid_credentials', message: '用户名或密码错误' } } },
    };
  },
  /** 提交被服务端接收。 */
  submitOk(feedbackId = 'fb-0001'): RouteRule {
    return { method: 'POST', path: '/api/feedback', handler: { status: 201, body: { feedbackId, status: 'processing' } } };
  },
  /** 提交结果未知（500）：未到达服务，草稿必须保留。 */
  submitServerError(): RouteRule {
    return { method: 'POST', path: '/api/feedback', handler: { status: 500, body: { error: { code: 'internal', message: '服务暂时不可用' } } } };
  },
  /** 查询进展：归档完成。 */
  recordArchived(id = 'fb-0001'): RouteRule {
    return {
      method: 'GET',
      path: `/api/feedback/${id}`,
      handler: { status: 200, body: { id, status: 'archived', createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), kaneoUrl: null } },
    };
  },
  /** 查询进展：服务端已接收但归档失败。 */
  recordArchiveFailed(id = 'fb-0001'): RouteRule {
    return {
      method: 'GET',
      path: `/api/feedback/${id}`,
      handler: {
        status: 200,
        body: { id, status: 'failed', createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), errorSummary: '归档失败：目标服务不可达' },
      },
    };
  },
  /** 会话查询（打开面板后刷新额度用）。 */
  sessionOk(): RouteRule {
    return {
      method: 'GET',
      path: '/api/auth/session',
      handler: {
        status: 200,
        body: {
          authenticated: true,
          kind: 'token',
          clientLabel: 'web:studytool',
          expiresAt: new Date(Date.now() + 3600_000).toISOString(),
          user: { id: 'fb-user-1', username: 'feedback-user', role: 'user' },
          quota: { dailyLimit: 20, used: 0, remaining: 20, resetAt: new Date(Date.now() + 3600_000).toISOString() },
        },
      },
    };
  },
};
