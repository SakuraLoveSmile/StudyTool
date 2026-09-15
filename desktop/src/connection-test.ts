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
      reason: err?.code ?? err?.name,
      duration_ms: durationMs,
    },
  };
}
