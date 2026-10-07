import { describe, expect, it } from 'vitest';
import {
  clamp,
  formatCurrency,
  formatPercent,
  formatQuantity,
  formatRatioPercent,
  formatRelativeTime,
  toNumber,
  toNumberOrZero,
  toneOf,
  toneTextClass,
} from './format';

/**
 * The formatters are the frontend's most load-bearing pure code.
 *
 * They exist because every money value crosses the wire as a *decimal string*
 * (the backend uses `Decimal`, and a float would lose cents on large balances).
 * The documented contract is that unparseable input renders as an em dash and
 * never as `NaN` or `undefined` — a promise worth testing, because `NaN` on a
 * dashboard looks like a crashed application rather than a missing number.
 */
describe('toNumber', () => {
  it('parses the decimal strings the API actually sends', () => {
    expect(toNumber('23565.84')).toBe(23565.84);
    expect(toNumber('-0.88')).toBe(-0.88);
    expect(toNumber('0')).toBe(0);
  });

  it('returns null rather than NaN for anything unparseable', () => {
    for (const input of ['', '   ', 'abc', 'NaN', 'Infinity', '-Infinity', null, undefined]) {
      expect(toNumber(input as never)).toBeNull();
    }
  });

  it('rejects non-finite numbers instead of passing them through', () => {
    expect(toNumber(Number.NaN)).toBeNull();
    expect(toNumber(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it('tolerates surrounding whitespace', () => {
    expect(toNumber('  42.50  ')).toBe(42.5);
  });
});

describe('toNumberOrZero', () => {
  it('substitutes zero so arithmetic cannot produce NaN', () => {
    expect(toNumberOrZero(null)).toBe(0);
    expect(toNumberOrZero('nonsense')).toBe(0);
    expect(toNumberOrZero('1.5')).toBe(1.5);
  });
});

describe('formatCurrency', () => {
  it('formats a decimal string as USD', () => {
    expect(formatCurrency('23565.84')).toBe('$23,565.84');
  });

  it('renders the fallback for unparseable input, never NaN', () => {
    expect(formatCurrency(null)).toBe('—');
    expect(formatCurrency('abc')).toBe('—');
    expect(formatCurrency(undefined, { fallback: 'n/a' })).toBe('n/a');
  });

  it('adds a plus sign only for positive values when signed', () => {
    expect(formatCurrency('10.00', { signed: true })).toBe('+$10.00');
    expect(formatCurrency('-10.00', { signed: true })).toBe('-$10.00');
    expect(formatCurrency('0.00', { signed: true })).toBe('$0.00');
  });

  it('abbreviates large magnitudes in compact mode', () => {
    expect(formatCurrency('1500000', { compact: true })).toBe('$1.50M');
    expect(formatCurrency('2500', { compact: true })).toBe('$2.50K');
    expect(formatCurrency('999', { compact: true })).toBe('$999.00');
  });
});

describe('formatQuantity', () => {
  it('trims trailing zeros so whole units read as whole numbers', () => {
    expect(formatQuantity('10.00000000')).toBe('10');
    expect(formatQuantity('0.03465522')).toBe('0.03465522');
  });

  it('falls back rather than rendering NaN', () => {
    expect(formatQuantity(null)).toBe('—');
  });
});

describe('formatPercent / formatRatioPercent', () => {
  it('formats a percentage that is already scaled, signed by default', () => {
    // Signed by default, unlike formatCurrency: a percentage on this dashboard
    // is almost always a change, where the sign is the point. Explicitly
    // unsigned for the settings that are levels rather than movements.
    expect(formatPercent('5.00')).toBe('+5.00%');
    expect(formatPercent('5.00', { signed: false })).toBe('5.00%');
    expect(formatPercent('-5.00')).toBe('-5.00%');
    expect(formatPercent('0.00')).toBe('0.00%');
  });

  it('converts a ratio to a percentage', () => {
    expect(formatRatioPercent(0.5)).toBe('50.0%');
  });

  it('never renders NaN', () => {
    expect(formatPercent(null)).toBe('—');
    expect(formatRatioPercent(null)).toBe('—');
  });
});

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-10-07T12:00:00Z');

  it('uses the compact forms the feed actually displays', () => {
    // Pinned exactly: the thoughts feed is narrow and these strings sit in a
    // fixed-width column, so "30 minutes ago" would wrap and change the layout.
    expect(formatRelativeTime('2026-10-07T11:59:59Z', now)).toBe('just now');
    expect(formatRelativeTime('2026-10-07T11:59:30Z', now)).toBe('30s ago');
    expect(formatRelativeTime('2026-10-07T11:30:00Z', now)).toBe('30m ago');
    expect(formatRelativeTime('2026-10-07T09:00:00Z', now)).toBe('3h ago');
    expect(formatRelativeTime('2026-10-04T12:00:00Z', now)).toBe('3d ago');
  });

  it('describes a future timestamp as ahead rather than negative ago', () => {
    // Clock skew between the browser and the server is normal; "-4m ago" would
    // read as a bug to a user.
    expect(formatRelativeTime('2026-10-07T12:00:30Z', now)).toBe('in a moment');
    expect(formatRelativeTime('2026-10-07T12:30:00Z', now)).toBe('in 30m');
    expect(formatRelativeTime('2026-10-07T14:00:00Z', now)).toBe('in 2h');
  });

  it('returns a placeholder for missing or invalid input rather than "Invalid Date"', () => {
    expect(formatRelativeTime(null, now)).toBe('—');
    expect(formatRelativeTime(undefined, now)).toBe('—');
    expect(formatRelativeTime('not-a-date', now)).toBe('—');
  });
});

describe('tone', () => {
  it('classifies positive, negative and neutral values', () => {
    expect(toneOf('1.00')).toBe('positive');
    expect(toneOf('-1.00')).toBe('negative');
    expect(toneOf('0.00')).toBe('neutral');
    // A missing value is neutral, not negative: colouring absent data red would
    // report a loss that has not happened.
    expect(toneOf(null)).toBe('neutral');
  });

  it('maps a tone to a text class', () => {
    expect(toneTextClass('1')).toContain('emerald');
    expect(toneTextClass('-1')).toContain('rose');
    expect(toneTextClass(null)).toContain('slate');
  });
});

describe('clamp', () => {
  it('bounds a value to the range', () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-1, 0, 10)).toBe(0);
    expect(clamp(11, 0, 10)).toBe(10);
  });
});

describe('a hostile payload cannot put NaN on the screen', () => {
  // The regression this guards: an undefined metric used to reach the DOM and
  // render as "NaN", which reads as a crash rather than as missing data. Every
  // formatter must degrade to a visible placeholder.
  const hostile = [null, undefined, '', 'abc', 'NaN', Number.NaN, Number.POSITIVE_INFINITY];

  it.each(hostile)('formatCurrency(%p) is not NaN', (value) => {
    expect(formatCurrency(value as never)).not.toMatch(/NaN/);
  });

  it.each(hostile)('formatPercent(%p) is not NaN', (value) => {
    expect(formatPercent(value as never)).not.toMatch(/NaN/);
  });

  it.each(hostile)('formatQuantity(%p) is not NaN', (value) => {
    expect(formatQuantity(value as never)).not.toMatch(/NaN/);
  });
});
