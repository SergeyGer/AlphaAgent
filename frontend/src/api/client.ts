import type {
  ApiErrorPayload,
  DecisionLog,
  ListResponse,
  LoginResponse,
  MarketNewsReport,
  Portfolio,
  PortfolioSnapshot,
  Recommendation,
  RecommendationDecisionResponse,
  RunAgentResponse,
  SnapshotRange,
  ToggleAutonomyResponse,
  Transaction,
} from '../types';

/* ------------------------------------------------------------------ */
/* Base URL + token storage                                            */
/* ------------------------------------------------------------------ */

const RAW_BASE: string = import.meta.env.VITE_API_BASE ?? '';

/** Normalised API base (no trailing slash). Empty string ⇒ same-origin. */
export const API_BASE: string = RAW_BASE.endsWith('/') ? RAW_BASE.slice(0, -1) : RAW_BASE;

const TOKEN_KEY = 'alphaagent.token';
const USERNAME_KEY = 'alphaagent.username';

function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode / disabled cookies) — degrade to memory */
  }
}

let currentToken: string | null = safeGet(TOKEN_KEY);

export function getToken(): string | null {
  return currentToken;
}

export function getStoredUsername(): string | null {
  return safeGet(USERNAME_KEY);
}

/** Persist (or clear) the auth token. Cleared tokens fire `onUnauthorized`. */
export function setToken(token: string | null, username?: string): void {
  currentToken = token;
  safeSet(TOKEN_KEY, token);
  safeSet(USERNAME_KEY, token === null ? null : (username ?? safeGet(USERNAME_KEY)));
}

/* ------------------------------------------------------------------ */
/* Unauthorized (401) broadcast                                        */
/* ------------------------------------------------------------------ */

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** Subscribe to 401 responses so the app can drop back to the login screen. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

function emitUnauthorized(): void {
  for (const listener of Array.from(unauthorizedListeners)) {
    try {
      listener();
    } catch {
      /* a broken listener must never mask the original API error */
    }
  }
}

/* ------------------------------------------------------------------ */
/* Errors                                                              */
/* ------------------------------------------------------------------ */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly fieldErrors: Record<string, string[] | string> | null;
  readonly payload: ApiErrorPayload | null;

  constructor(
    status: number,
    detail: string,
    fieldErrors: Record<string, string[] | string> | null = null,
    payload: ApiErrorPayload | null = null,
  ) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.fieldErrors = fieldErrors;
    this.payload = payload;
  }

  /** True when the resource was already decided (HTTP 409). */
  get isConflict(): boolean {
    return this.status === 409;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return 'Unexpected error';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function extractDetail(body: unknown, status: number): string {
  if (isRecord(body)) {
    const detail = body['detail'];
    if (typeof detail === 'string' && detail.trim() !== '') return detail;
    const errors = body['errors'];
    if (isRecord(errors)) {
      for (const value of Object.values(errors)) {
        if (typeof value === 'string' && value.trim() !== '') return value;
        if (Array.isArray(value) && typeof value[0] === 'string') return value[0];
      }
    }
    const nonField = body['non_field_errors'];
    if (Array.isArray(nonField) && typeof nonField[0] === 'string') return nonField[0];
    if (typeof nonField === 'string') return nonField;
  }
  if (status === 403) return 'You do not have permission to perform this action.';
  if (status === 404) return 'Resource not found.';
  if (status === 409) return 'This item has already been decided.';
  if (status >= 500) return 'The server encountered an error. Please retry.';
  return `Request failed with status ${status}.`;
}

/* ------------------------------------------------------------------ */
/* Request core                                                        */
/* ------------------------------------------------------------------ */

type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  /** Attach the `Authorization: Token <token>` header (default `true`). */
  auth?: boolean;
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue;
      search.append(key, String(value));
    }
  }
  const qs = search.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, signal, auth = true } = options;

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth && currentToken) headers['Authorization'] = `Token ${currentToken}`;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: 'same-origin',
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ApiError(0, 'Network error — the API is unreachable.');
  }

  if (response.status === 401) {
    setToken(null);
    emitUnauthorized();
    throw new ApiError(401, 'Your session expired. Please sign in again.');
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let data: unknown = null;
  if (text !== '') {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      data = null;
    }
  }

  if (!response.ok) {
    const payload = isRecord(data) ? (data as ApiErrorPayload) : null;
    const fieldErrors = payload && isRecord(payload.errors) ? payload.errors : null;
    throw new ApiError(response.status, extractDetail(data, response.status), fieldErrors, payload);
  }

  return data as T;
}

/* ------------------------------------------------------------------ */
/* Normalisers — tolerate paginated and bare-array list responses      */
/* ------------------------------------------------------------------ */

export function unwrapList<T>(payload: ListResponse<T> | null | undefined): T[] {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.results)) return payload.results;
  return [];
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                           */
/* ------------------------------------------------------------------ */

const HOURS_BY_RANGE: Record<Exclude<SnapshotRange, 'ALL'>, number> = {
  '24H': 24,
  '7D': 24 * 7,
  '30D': 24 * 30,
};

export const api = {
  /** `POST /api/auth/token/` → `{ token }` */
  login(username: string, password: string): Promise<LoginResponse> {
    return apiRequest<LoginResponse>('/api/auth/token/', {
      method: 'POST',
      body: { username, password },
      auth: false,
    });
  },

  /** `GET /api/portfolio/` */
  portfolio(signal?: AbortSignal): Promise<Portfolio> {
    return apiRequest<Portfolio>('/api/portfolio/', { signal });
  },

  /** `GET /api/portfolio/snapshots/?hours=` — oldest first. `ALL` omits the param. */
  snapshots(range: SnapshotRange, signal?: AbortSignal): Promise<ListResponse<PortfolioSnapshot>> {
    const query = range === 'ALL' ? undefined : { hours: HOURS_BY_RANGE[range] };
    return apiRequest<ListResponse<PortfolioSnapshot>>('/api/portfolio/snapshots/', { query, signal });
  },

  /** `GET /api/portfolio/transactions/` */
  transactions(signal?: AbortSignal): Promise<ListResponse<Transaction>> {
    return apiRequest<ListResponse<Transaction>>('/api/portfolio/transactions/', { signal });
  },

  /** `GET /api/portfolio/logs/` */
  logs(signal?: AbortSignal): Promise<ListResponse<DecisionLog>> {
    return apiRequest<ListResponse<DecisionLog>>('/api/portfolio/logs/', { signal });
  },

  /** `GET /api/portfolio/recommendations/` */
  recommendations(signal?: AbortSignal): Promise<ListResponse<Recommendation>> {
    return apiRequest<ListResponse<Recommendation>>('/api/portfolio/recommendations/', { signal });
  },

  /** `GET /api/market/news/?ticker=&limit=` — headlines split by sentiment. */
  news(ticker: string, limit = 10, signal?: AbortSignal): Promise<MarketNewsReport> {
    return apiRequest<MarketNewsReport>('/api/market/news/', {
      query: { ticker: ticker.trim().toUpperCase(), limit },
      signal,
    });
  },

  /** `POST /api/portfolio/recommendations/<id>/approve/` */
  approveRecommendation(id: number): Promise<RecommendationDecisionResponse> {
    return apiRequest<RecommendationDecisionResponse>(
      `/api/portfolio/recommendations/${id}/approve/`,
      { method: 'POST' },
    );
  },

  /** `POST /api/portfolio/recommendations/<id>/reject/` */
  rejectRecommendation(id: number): Promise<RecommendationDecisionResponse> {
    return apiRequest<RecommendationDecisionResponse>(
      `/api/portfolio/recommendations/${id}/reject/`,
      { method: 'POST' },
    );
  },

  /** `POST /api/portfolio/toggle-autonomy/` */
  toggleAutonomy(isAutonomous: boolean): Promise<ToggleAutonomyResponse> {
    return apiRequest<ToggleAutonomyResponse>('/api/portfolio/toggle-autonomy/', {
      method: 'POST',
      body: { is_autonomous: isAutonomous, confirm: true },
    });
  },

  /** `POST /api/portfolio/run-agent/` → 202 `{ status, task_id, portfolio_id }` */
  runAgent(): Promise<RunAgentResponse> {
    return apiRequest<RunAgentResponse>('/api/portfolio/run-agent/', { method: 'POST' });
  },
};

/* ------------------------------------------------------------------ */
/* WebSocket URL                                                       */
/* ------------------------------------------------------------------ */

const RAW_WS_BASE: string = import.meta.env.VITE_WS_BASE ?? '';

/**
 * Build `ws(s)://<host>/ws/portfolio/?token=<token>`.
 * Derived from `VITE_WS_BASE`, else `VITE_API_BASE` when absolute, else the
 * current page origin (the Vite dev proxy forwards `/ws` to Django).
 */
export function buildPortfolioSocketUrl(token: string): string {
  if (RAW_WS_BASE) {
    return appendToken(RAW_WS_BASE, token);
  }

  let origin: string;
  let basePath = '';
  if (/^https?:\/\//i.test(API_BASE)) {
    const url = new URL(API_BASE);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    origin = url.origin;
    basePath = url.pathname.replace(/\/$/, '');
  } else {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    origin = `${protocol}//${window.location.host}`;
    basePath = API_BASE;
  }

  return appendToken(`${origin}${basePath}/ws/portfolio/`, token);
}

function appendToken(base: string, token: string): string {
  const url = new URL(base, window.location.href);
  url.searchParams.set('token', token);
  return url.toString();
}
