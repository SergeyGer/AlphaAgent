import type { Decimal } from '../types';

/**
 * Any decimal-ish value that may arrive from the API (or from an optimistic
 * local update). The contract says strings; numbers are tolerated defensively.
 */
export type DecimalInput = Decimal | number | null | undefined;

const NUMBER_LOCALE = 'en-US';

/** Parse a decimal-ish value. Returns `null` instead of `NaN`/`Infinity`. */
export function toNumber(value: DecimalInput): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const trimmed = value.trim();
  if (trimmed === '') return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Parse, falling back to `0` — for arithmetic where a missing value is zero. */
export function toNumberOrZero(value: DecimalInput): number {
  return toNumber(value) ?? 0;
}

const currencyFormatters = new Map<string, Intl.NumberFormat>();

function currencyFormatter(min: number, max: number): Intl.NumberFormat {
  const key = `${min}:${max}`;
  const cached = currencyFormatters.get(key);
  if (cached) return cached;
  const formatter = new Intl.NumberFormat(NUMBER_LOCALE, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: min,
    maximumFractionDigits: max,
  });
  currencyFormatters.set(key, formatter);
  return formatter;
}

export interface CurrencyOptions {
  /** Use `$1.23M` style compact notation for large magnitudes. */
  compact?: boolean;
  /** Always render an explicit `+` for positive values. */
  signed?: boolean;
  /** Fraction digits (defaults: 2 for normal, 2 for compact). */
  decimals?: number;
  /** Value rendered when the input cannot be parsed. Default `'—'`. */
  fallback?: string;
}

const COMPACT_UNITS: ReadonlyArray<{ limit: number; suffix: string }> = [
  { limit: 1e12, suffix: 'T' },
  { limit: 1e9, suffix: 'B' },
  { limit: 1e6, suffix: 'M' },
  { limit: 1e3, suffix: 'K' },
];

function formatCompact(value: number, decimals: number): string {
  const abs = Math.abs(value);
  for (const unit of COMPACT_UNITS) {
    if (abs >= unit.limit) {
      const scaled = value / unit.limit;
      return `$${scaled.toFixed(decimals)}${unit.suffix}`;
    }
  }
  return currencyFormatter(decimals, decimals).format(value);
}

/**
 * Format a money value, e.g. `"23565.84"` → `"$23,565.84"`.
 * Never returns `NaN`/`undefined`: unparseable input yields `'—'`.
 */
export function formatCurrency(value: DecimalInput, options: CurrencyOptions = {}): string {
  const { compact = false, signed = false, fallback = '—' } = options;
  const parsed = toNumber(value);
  if (parsed === null) return fallback;
  const decimals = options.decimals ?? 2;
  const body = compact ? formatCompact(parsed, decimals) : currencyFormatter(decimals, decimals).format(parsed);
  if (signed && parsed > 0) return `+${body}`;
  return body;
}

/**
 * Format a quantity with trailing zeros trimmed, e.g. `"0.03465522"` → `"0.03465522"`,
 * `"10.00000000"` → `"10"`.
 */
export function formatQuantity(value: DecimalInput, maxDecimals = 8): string {
  const parsed = toNumber(value);
  if (parsed === null) return '—';
  if (parsed !== 0 && Math.abs(parsed) < 1e-8) return '<0.00000001';
  const decimals = Math.abs(parsed) >= 1000 ? Math.min(maxDecimals, 4) : maxDecimals;
  const fixed = parsed.toFixed(decimals);
  const trimmed = fixed.includes('.') ? fixed.replace(/0+$/, '').replace(/\.$/, '') : fixed;
  return Number(trimmed).toLocaleString(NUMBER_LOCALE, { maximumFractionDigits: decimals });
}

export interface PercentOptions {
  /** Render an explicit `+` for positive values (default `true`). */
  signed?: boolean;
  decimals?: number;
  fallback?: string;
}

/**
 * Format a percentage that is already expressed in percent units, e.g.
 * `"0.46"` → `"+0.46%"`.
 */
export function formatPercent(value: DecimalInput, options: PercentOptions = {}): string {
  const { signed = true, decimals = 2, fallback = '—' } = options;
  const parsed = toNumber(value);
  if (parsed === null) return fallback;
  const sign = signed && parsed > 0 ? '+' : '';
  return `${sign}${parsed.toFixed(decimals)}%`;
}

/** `0.42` → `"42.0%"` — for ratios that are *not* pre-multiplied. */
export function formatRatioPercent(ratio: DecimalInput, decimals = 1): string {
  const parsed = toNumber(ratio);
  if (parsed === null) return '—';
  return `${(parsed * 100).toFixed(decimals)}%`;
}

/**
 * Fixed-point value with an explicit `+` for positives, e.g. `3` → `"+3.00"`,
 * `-2.5` → `"-2.50"`. Used for signed polarity badges.
 */
export function formatSignedFixed(value: DecimalInput, decimals = 2, fallback = '—'): string {
  const parsed = toNumber(value);
  if (parsed === null) return fallback;
  // Normalise `-0` so it never renders as "-0.00".
  const safe = parsed === 0 ? 0 : parsed;
  const sign = safe > 0 ? '+' : '';
  return `${sign}${safe.toFixed(decimals)}`;
}

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3_600;
const SECONDS_PER_DAY = 86_400;

/** `"2026-09-30T00:00:00Z"` → `"12s ago"`, `"5m ago"`, `"3h ago"`, `"2d ago"`. */
export function formatRelativeTime(value: string | null | undefined, now: number = Date.now()): string {
  const date = parseDate(value);
  if (!date) return '—';
  const deltaSeconds = Math.round((now - date.getTime()) / 1000);

  if (deltaSeconds < 0) {
    const ahead = Math.abs(deltaSeconds);
    if (ahead < SECONDS_PER_MINUTE) return 'in a moment';
    if (ahead < SECONDS_PER_HOUR) return `in ${Math.floor(ahead / SECONDS_PER_MINUTE)}m`;
    if (ahead < SECONDS_PER_DAY) return `in ${Math.floor(ahead / SECONDS_PER_HOUR)}h`;
    return `in ${Math.floor(ahead / SECONDS_PER_DAY)}d`;
  }

  if (deltaSeconds < 5) return 'just now';
  if (deltaSeconds < SECONDS_PER_MINUTE) return `${deltaSeconds}s ago`;
  if (deltaSeconds < SECONDS_PER_HOUR) return `${Math.floor(deltaSeconds / SECONDS_PER_MINUTE)}m ago`;
  if (deltaSeconds < SECONDS_PER_DAY) return `${Math.floor(deltaSeconds / SECONDS_PER_HOUR)}h ago`;
  return `${Math.floor(deltaSeconds / SECONDS_PER_DAY)}d ago`;
}

/** `"2026-09-30T14:03:22Z"` → `"14:03:22"` (local time). */
export function formatClockTime(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return '—';
  return date.toLocaleTimeString(NUMBER_LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** `"2026-09-30T14:03:22Z"` → `"Sep 30, 14:03"`. */
export function formatDateTime(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return '—';
  return date.toLocaleString(NUMBER_LOCALE, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** Short axis label for the equity chart, adapted to the selected range. */
export function formatAxisTime(value: string | number | null | undefined, withDate: boolean): string {
  const date = typeof value === 'number' ? new Date(value) : parseDate(value ?? null);
  if (!date) return '';
  if (withDate) {
    return date.toLocaleDateString(NUMBER_LOCALE, { month: 'short', day: 'numeric' });
  }
  return date.toLocaleTimeString(NUMBER_LOCALE, { hour: '2-digit', minute: '2-digit', hour12: false });
}

export type Tone = 'positive' | 'negative' | 'neutral';

/** Classify a numeric value into a semantic tone (emerald / rose / slate). */
export function toneOf(value: DecimalInput): Tone {
  const parsed = toNumber(value);
  if (parsed === null || parsed === 0) return 'neutral';
  return parsed > 0 ? 'positive' : 'negative';
}

export const TONE_TEXT_CLASS: Record<Tone, string> = {
  positive: 'text-emerald-400',
  negative: 'text-rose-400',
  neutral: 'text-slate-400',
};

export function toneTextClass(value: DecimalInput): string {
  return TONE_TEXT_CLASS[toneOf(value)];
}

/** Clamp helper used by progress bars. */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
