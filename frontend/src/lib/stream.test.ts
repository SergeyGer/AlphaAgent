import { describe, expect, it } from 'vitest';
import { parseStreamMessage } from './stream';

/**
 * The WebSocket envelope validator.
 *
 * This is the frontend's only defence between an arbitrary frame and the
 * dashboard's state. It returns `null` for anything malformed so the socket loop
 * can ignore it, which matters because the alternative — letting a bad frame
 * through — writes `undefined` into state and blanks a panel. That is exactly the
 * class of bug that once emptied the metric cards, and it presented as "the
 * dashboard is broken" rather than as a parse error.
 */
describe('parseStreamMessage', () => {
  const frame = (type: unknown, payload: unknown): string => JSON.stringify({ type, payload });

  it('accepts a well-formed frame', () => {
    const parsed = parseStreamMessage(frame('portfolio.snapshot', { metrics: {} }));
    expect(parsed).not.toBeNull();
    expect(parsed?.type).toBe('portfolio.snapshot');
  });

  it.each([
    'connection.established',
    'portfolio.snapshot',
    'trade.executed',
    'decision.created',
    'agent.thinking',
    'recommendation.created',
    'recommendation.updated',
    'autonomy.changed',
    'budget.updated',
    'budget.exhausted',
  ])('accepts the known type %s', (type) => {
    expect(parseStreamMessage(frame(type, {}))).not.toBeNull();
  });

  it('rejects a type the frontend does not know', () => {
    // A newer backend must not be able to push a frame that this client would
    // then handle as `undefined`.
    expect(parseStreamMessage(frame('something.new', {}))).toBeNull();
  });

  it('rejects a non-string frame', () => {
    for (const raw of [null, undefined, 42, {}, [], true]) {
      expect(parseStreamMessage(raw)).toBeNull();
    }
  });

  it('rejects invalid JSON without throwing', () => {
    expect(parseStreamMessage('{not json')).toBeNull();
    expect(parseStreamMessage('')).toBeNull();
  });

  it('rejects a JSON value that is not an object', () => {
    expect(parseStreamMessage('"a string"')).toBeNull();
    expect(parseStreamMessage('[1,2,3]')).toBeNull();
    expect(parseStreamMessage('null')).toBeNull();
  });

  it('rejects a missing or non-object payload', () => {
    expect(parseStreamMessage(JSON.stringify({ type: 'trade.executed' }))).toBeNull();
    expect(parseStreamMessage(frame('trade.executed', null))).toBeNull();
    expect(parseStreamMessage(frame('trade.executed', 'nope'))).toBeNull();
    expect(parseStreamMessage(frame('trade.executed', [1, 2]))).toBeNull();
  });

  it('rejects a missing or non-string type', () => {
    expect(parseStreamMessage(JSON.stringify({ payload: {} }))).toBeNull();
    expect(parseStreamMessage(frame(42, {}))).toBeNull();
  });

  it('does not mutate or throw on a payload with hostile keys', () => {
    const parsed = parseStreamMessage(frame('decision.created', { __proto__: { polluted: true } }));
    expect(parsed).not.toBeNull();
    expect(({} as Record<string, unknown>)['polluted']).toBeUndefined();
  });
});
