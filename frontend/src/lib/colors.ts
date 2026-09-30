/** Chart + ticker colour palette (dark-terminal friendly, AA on slate-900). */
export const CHART_PALETTE: readonly string[] = [
  '#34d399',
  '#60a5fa',
  '#fbbf24',
  '#a78bfa',
  '#f472b6',
  '#22d3ee',
  '#4ade80',
  '#fb7185',
  '#facc15',
  '#38bdf8',
  '#c084fc',
  '#2dd4bf',
];

/** Reserved colour for the synthetic "Cash" slice. */
export const CASH_COLOR = '#475569';

export function paletteColor(index: number): string {
  const size = CHART_PALETTE.length;
  const safeIndex = ((index % size) + size) % size;
  return CHART_PALETTE[safeIndex] ?? CASH_COLOR;
}

export const ACCENT = {
  emerald: '#34d399',
  rose: '#fb7185',
  amber: '#fbbf24',
  sky: '#38bdf8',
  slate: '#64748b',
} as const;

export function toneAccent(value: number): string {
  if (value > 0) return ACCENT.emerald;
  if (value < 0) return ACCENT.rose;
  return ACCENT.slate;
}
