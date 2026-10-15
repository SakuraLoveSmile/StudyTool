/**
 * Classifies the outcome of a `/api/desktop/test-connection` probe so the main process can
 * log a failure that distinguishes the two classes a user actually experiences:
 *
 *   - `connection_test.unreachable` — the local backend could not be reached at all
 *     (transport error: connection refused, timeout, socket closed);
 *   - `connection_test.failed` — the local backend answered, but its probe of the external
 *     service failed (bad key, upstream 5xx, timeout reaching the LLM, ...).
 *
 * Keeping this pure and separate from `ipcMain` is what makes the distinction testable.
 */

export type ConnectionTestEvent =
  | 'connection_test.failed'
  | 'connection_test.unreachable'
  | 'connection_test.invalid_response';

export interface ConnectionTestOutcome {
  /** Payload handed back to the renderer. */
  result: Record<string, unknown>;
  /** Diagnostic event to emit, or null when the probe succeeded. */
  event: ConnectionTestEvent | null;
  message: string;
  fields: Record<string, unknown>;
}

/** 主进程请求本地后端的默认等待上限（毫秒）：除 OCR 连接测试外全部沿用该值。 */
export const DEFAULT_BACKEND_REQUEST_TIMEOUT_MS = 15_000;

/**
 * PP-OCRv6 是异步 jobs 协议：本地后端提交识别任务后必须轮询百度侧结果，一次探针要等
 * 排队 + 识别 + 回查，15 秒必然误报失败。315 秒（5 分 15 秒）覆盖完整往返并留出余量。
 */
export const OCR_BACKEND_REQUEST_TIMEOUT_MS = 315_000;

/** 请求超时的专用错误码：让分类结果能区分“本地后端没在时限内回应”与其他传输失败。 */
export const BACKEND_REQUEST_TIMEOUT_CODE = 'ETIMEDOUT';

const OCR_SERVICE_TYPES = new Set(['baidu_ocr', 'ocr']);

/** `baidu_ocr` 是设置页使用的规范名，`ocr` 是后端同样接受的历史别名。 */
export function isOcrServiceType(serviceType: string | null | undefined): boolean {
  return OCR_SERVICE_TYPES.has(String(serviceType ?? '').trim().toLowerCase());
}

/** 只有 OCR 连接测试放宽等待上限，其余服务（llm / embedding / 未知服务）保持默认 15 秒。 */
export function resolveConnectionTestTimeoutMs(serviceType: string | null | undefined): number {
  return isOcrServiceType(serviceType)
    ? OCR_BACKEND_REQUEST_TIMEOUT_MS
    : DEFAULT_BACKEND_REQUEST_TIMEOUT_MS;
}

/** 超时错误：带可读中文说明与稳定错误码，供分类与日志使用。 */
export function createBackendRequestTimeoutError(
  timeoutMs: number,
  target: string
): Error & { code: string } {
  const seconds = Math.round(timeoutMs / 1000);
  const error = new Error(`请求本地后端超时（等待 ${seconds} 秒未收到响应）：${target}`) as Error & {
    code: string;
  };
  error.name = 'TimeoutError';
  error.code = BACKEND_REQUEST_TIMEOUT_CODE;
  return error;
}

/**
 * Node 的 `timeout` 选项只会在超时时发出 'timeout' 事件，并不会关闭请求：不显式
 * `req.destroy()` 就会留下悬挂 socket，渲染端只能一直等到自己的超时，本地后端也可能
 * 继续被占用。这里把超时变成一次真正的终止：destroy 随之上抛 'error'，由
 * `classifyConnectionTestTransportFailure` 归类成可读的失败结果。
 */
export function terminateRequestOnTimeout(
  req: {
    on(event: 'timeout', listener: () => void): unknown;
    destroy(error?: Error): unknown;
  },
  timeoutMs: number,
  target: string
): void {
  req.on('timeout', () => {
    req.destroy(createBackendRequestTimeoutError(timeoutMs, target));
  });
}

function asRecord(value: unknown): Record<string, any> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : null;
}

/**
 * Normalizes the backend reply (any HTTP status) into the renderer envelope plus the
 * failure record worth logging.
 */
export function normalizeConnectionTestResult(
  serviceType: string,
  status: number,
  data: unknown,
  durationMs: number
): ConnectionTestOutcome {
  const payload = asRecord(data);

  if (!payload) {
    return {
      result: {
        ok: false,
        service: serviceType,
        error_type: 'invalid_backend_response',
        message: `本地后端返回了无法解析的响应（HTTP ${status}）`,
      },
      event: 'connection_test.invalid_response',
      message: `连接测试返回了非 JSON 响应: ${serviceType}`,
      fields: {
        service: serviceType,
        error_type: 'invalid_backend_response',
        status_code: status,
        duration_ms: durationMs,
      },
    };
  }

  // A FastAPI HTTPException body (`{detail}`) is not the probe envelope. Normalize it so the
  // settings page shows the backend's reason instead of an empty message.
  if (payload.ok === undefined && typeof payload.detail === 'string') {
    return {
      result: { ok: false, service: serviceType, error_type: 'backend_error', message: payload.detail },
      event: 'connection_test.failed',
      message: `连接测试未通过: ${serviceType}`,
      fields: {
        service: serviceType,
        error_type: 'backend_error',
        status_code: status,
        duration_ms: durationMs,
      },
    };
  }

  if (payload.ok === false || status >= 400) {
    return {
      result: payload,
      event: 'connection_test.failed',
      message: `连接测试未通过: ${serviceType}`,
      fields: {
        service: serviceType,
        error_type: payload.error_type ?? 'backend_error',
        reason_code: payload.reason_code,
        status_code: payload.status_code ?? status,
        call_id: payload.call_id,
        duration_ms: durationMs,
      },
    };
  }

  return { result: payload, event: null, message: '', fields: {} };
}

/**
 * Builds the failure record for a transport-level error, i.e. the local backend itself was
 * never reached. `err.code` (e.g. `ECONNREFUSED`, `ETIMEDOUT`) is the useful diagnostic.
 */
export interface ConnectionTestTransportFailure extends ConnectionTestOutcome {
  event: 'connection_test.unreachable';
}

export function classifyConnectionTestTransportFailure(
  serviceType: string,
  err: { code?: string; name?: string } | null | undefined,
  durationMs: number
): ConnectionTestTransportFailure {
  const reason = err?.code ?? err?.name;

  // OCR 走异步 jobs 识别，等待上限被单独放宽：超时时要告诉用户“等到了 315 秒”，
  // 而不是让他以为本地后端无法连接。
  if (isOcrServiceType(serviceType) && err?.code === BACKEND_REQUEST_TIMEOUT_CODE) {
    const seconds = Math.round(OCR_BACKEND_REQUEST_TIMEOUT_MS / 1000);
    return {
      result: {
        ok: false,
        service: serviceType,
        error_type: 'backend_unreachable',
        message: `百度 PP-OCRv6 识别等待超时（已等待 ${seconds} 秒仍未返回结果），请稍后重试或查看运行日志`,
      },
      event: 'connection_test.unreachable',
      message: `连接测试等待超时: ${serviceType}`,
      fields: {
        service: serviceType,
        error_type: 'backend_unreachable',
        reason,
        timeout: true,
        wait_limit_ms: OCR_BACKEND_REQUEST_TIMEOUT_MS,
        duration_ms: durationMs,
      },
    };
  }

  return {
    result: {
      ok: false,
      service: serviceType,
      error_type: 'backend_unreachable',
      message: '无法请求本地后端服务，请稍后重试或查看运行日志',
    },
    event: 'connection_test.unreachable',
    message: `无法请求本地后端: ${serviceType}`,
    fields: {
      service: serviceType,
      error_type: 'backend_unreachable',
      reason,
      duration_ms: durationMs,
    },
  };
}
