import type { StreamMessage, StreamMessageType } from '../types';

/**
 * Every frame `type` the backend may push. `connection.established` is the
 * greeting sent immediately after the socket is accepted; it carries no
 * dashboard state but must not be dropped as unknown.
 */
const KNOWN_TYPES: ReadonlySet<string> = new Set<StreamMessageType>([
  'connection.established',
  'portfolio.snapshot',
  'trade.executed',
  'decision.created',
  'agent.thinking',
  'recommendation.created',
  'recommendation.updated',
  'autonomy.changed',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Validate a raw WebSocket frame against the frozen stream contract.
 * Returns `null` for anything malformed so the socket loop can ignore it.
 */
export function parseStreamMessage(raw: unknown): StreamMessage | null {
  if (typeof raw !== 'string') return null;

  let data: unknown;
  try {
    data = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }

  if (!isRecord(data)) return null;
  const type = data['type'];
  const payload = data['payload'];
  if (typeof type !== 'string' || !KNOWN_TYPES.has(type)) return null;
  if (!isRecord(payload)) return null;

  // The envelope (`type` + object `payload`) is validated above; payload
  // internals are trusted per the frozen contract and narrowed by the
  // discriminated union at each `switch (message.type)` site.
  return { type, payload } as unknown as StreamMessage;
}
