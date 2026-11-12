import type { ReactElement } from 'react';
import { cn } from '../lib/cn';
import { presentText, guardCheckOutcomeLabel, guardStatusLabel } from '../lib/decisionRuns';
import { formatQuantity, toNumber } from '../lib/format';
import type { GuardCheck, GuardComparison as GuardComparisonData } from '../types';

/**
 * The deterministic guard's requested-vs-allowed comparison.
 *
 * `not_evaluated` is rendered as its own claim — a soft amber "Not evaluated"
 * with no pass/fail implication — because a check the guard never ran is
 * neither evidence for nor against the order. Collapsing it into "Fail" would
 * report a refusal that did not happen.
 */

export interface GuardComparisonProps {
  guard: GuardComparisonData | null;
  className?: string;
}

type GuardTone = 'approved' | 'clamped' | 'rejected';

const STATUS_CLASS: Record<GuardTone, string> = {
  approved: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  clamped: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  rejected: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
};

const STATUS_TEXT: Record<GuardTone, string> = {
  approved: 'The requested order passed every guard check that was evaluated.',
  clamped: 'The guard reduced the requested order to the allowed quantity.',
  rejected: 'The guard refused the requested order; no quantity was allowed.',
};

const OUTCOME_CLASS: Record<string, string> = {
  pass: 'text-emerald-300',
  fail: 'text-rose-300',
  not_evaluated: 'text-amber-200',
};

function guardTone(status: string | null | undefined): GuardTone | null {
  const key = (status ?? '').trim().toLowerCase();
  return key === 'approved' || key === 'clamped' || key === 'rejected' ? key : null;
}

/** A missing quantity is "Not available"; a measured `0` stays `0`. */
function formatGuardQuantity(value: string | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return 'Not available';
  return formatQuantity(parsed);
}

function checkValue(value: string | null | undefined, unit: string | null | undefined): string {
  const text = presentText(value);
  if (!text) return 'Not available';
  const suffix = presentText(unit);
  return suffix ? `${text} ${suffix}` : text;
}

function CheckOutcome({ check }: { check: GuardCheck }): ReactElement {
  const key = (check?.outcome ?? '').trim().toLowerCase();
  return (
    <span
      data-outcome={key || 'unknown'}
      className={cn(
        'inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[11px] font-semibold uppercase tracking-wider',
        key === 'pass' && 'border-emerald-500/30 bg-emerald-500/10',
        key === 'fail' && 'border-rose-500/30 bg-rose-500/10',
        key === 'not_evaluated' && 'border-amber-500/30 bg-amber-500/10',
        (key !== 'pass' && key !== 'fail' && key !== 'not_evaluated') &&
          'border-slate-600/40 bg-slate-700/20 text-slate-300',
        OUTCOME_CLASS[key] ?? 'text-slate-300',
      )}
    >
      {guardCheckOutcomeLabel(key)}
    </span>
  );
}

export function GuardComparison({ guard, className }: GuardComparisonProps): ReactElement {
  if (!guard) {
    return (
      <div
        data-state="guard-unavailable"
        className={cn(
          'rounded-lg border border-dashed border-slate-800 bg-slate-950/40 px-4 py-4',
          className,
        )}
      >
        <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
          Guard comparison not available
        </p>
        <p className="mt-1 text-sm leading-relaxed text-slate-400">
          This run has no stored guard comparison. No request is presented as approved by default.
        </p>
      </div>
    );
  }

  const partial = guard.partial === true;
  const status = guardTone(guard.status);
  const statusLabel = guardStatusLabel(guard.status);
  const checks = Array.isArray(guard.checks) ? guard.checks.filter(Boolean) : [];
  const passed = checks.filter((check) => (check.outcome ?? '').toLowerCase() === 'pass').length;
  const failed = checks.filter((check) => (check.outcome ?? '').toLowerCase() === 'fail').length;
  const notEvaluated = checks.filter(
    (check) => (check.outcome ?? '').toLowerCase() === 'not_evaluated',
  ).length;
  const unknown = checks.length - passed - failed - notEvaluated;
  const reasonCodes = Array.isArray(guard.reason_codes)
    ? guard.reason_codes.filter((code): code is string => typeof code === 'string' && code.trim() !== '')
    : [];
  const bindingRule = presentText(guard.binding_rule);
  const guardId = presentText(guard.id ?? null);
  const unavailableFieldList = Array.isArray(guard.unavailable_fields)
    ? guard.unavailable_fields.filter((field) => presentText(field) !== null).join(', ')
    : '';

  return (
    <div className={cn('min-w-0 space-y-4', className)}>
      <div className="flex flex-wrap items-center gap-2">
        <span
          data-testid="guard-status"
          className={cn(
            'inline-flex items-center rounded border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-wider',
            status ? STATUS_CLASS[status] : 'border-slate-600/40 bg-slate-700/20 text-slate-300',
          )}
        >
          {statusLabel}
        </span>
        <p className="text-sm leading-relaxed text-slate-300">
          {status ? STATUS_TEXT[status] : 'The guard result is not recognised by this version.'}
        </p>
      </div>

      {partial ? (
        <p
          data-state="guard-partial"
          className="rounded-lg border border-amber-500/30 bg-amber-500/[0.06] px-3 py-2 text-sm leading-relaxed text-amber-200"
        >
          This evaluation was recorded by a legacy event. Only the values that event carried are
          shown; {unavailableFieldList || 'the remaining fields'} are unavailable, not zero.
        </p>
      ) : null}

      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5">
          <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Requested quantity
          </dt>
          <dd className="mt-0.5 break-all font-mono text-lg font-semibold tabular-nums text-slate-100">
            {formatGuardQuantity(guard.requested_quantity)}
          </dd>
        </div>
        <div className="min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5">
          <dt className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Allowed quantity
          </dt>
          <dd className="mt-0.5 break-all font-mono text-lg font-semibold tabular-nums text-slate-100">
            {formatGuardQuantity(guard.allowed_quantity)}
          </dd>
        </div>
      </dl>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="min-w-0">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Quote time
          </p>
          <p className="mt-0.5 break-all font-mono text-sm text-slate-200">
            {presentText(guard.quote_time ?? null) ?? 'Unavailable'}
          </p>
        </div>
        <div className="min-w-0">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Evaluation
          </p>
          <p className="mt-0.5 break-all font-mono text-sm text-slate-200">
            {guardId ?? 'Identifier unavailable'}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="min-w-0">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Binding rule
          </p>
          <p className="mt-0.5 break-all font-mono text-sm text-slate-200">
            {bindingRule ?? 'No binding rule recorded'}
          </p>
        </div>
        <div className="min-w-0">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Reason codes
          </p>
          {reasonCodes.length === 0 ? (
            <p className="mt-0.5 text-sm text-slate-400">
              {partial ? 'Unavailable in this legacy event' : 'No reason codes recorded'}
            </p>
          ) : (
            <ul className="mt-1 flex flex-wrap gap-1.5">
              {reasonCodes.map((code) => (
                <li key={code}>
                  <span className="inline-flex rounded border border-slate-700 bg-slate-900 px-1.5 py-0.5 font-mono text-[11px] text-slate-300">
                    {code}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <section aria-label="Guard checks" className="min-w-0">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h3 className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            Checks
          </h3>
          <p className="font-mono text-[11px] tabular-nums text-slate-400">
            {checks.length} total · {passed} pass · {failed} fail · {notEvaluated} not evaluated
            {unknown > 0 ? ` · ${unknown} unknown` : ''}
          </p>
        </div>

        {checks.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">
            {partial
              ? 'Check details are unavailable in this legacy event; none are presented as passing.'
              : 'No guard checks were recorded, so none are presented as passing.'}
          </p>
        ) : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
              <caption className="sr-only">
                Per-check guard results, including checks that were not evaluated
              </caption>
              <thead>
                <tr className="border-b border-slate-800 font-mono text-[10px] uppercase tracking-[0.14em] text-slate-400">
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Rule
                  </th>
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Outcome
                  </th>
                  <th scope="col" className="py-1.5 pr-3 font-semibold">
                    Threshold
                  </th>
                  <th scope="col" className="py-1.5 font-semibold">
                    Actual
                  </th>
                </tr>
              </thead>
              <tbody>
                {checks.map((check, index) => {
                  const outcome = (check.outcome ?? '').trim().toLowerCase();
                  return (
                    <tr
                      key={`${check.rule_code || 'check'}-${index}`}
                      className={cn(
                        'border-b border-slate-800/60 align-top',
                        outcome === 'not_evaluated' && 'bg-amber-500/[0.03]',
                        outcome === 'fail' && 'bg-rose-500/[0.03]',
                      )}
                    >
                      <th
                        scope="row"
                        className="break-all py-2 pr-3 text-left font-mono text-xs font-medium text-slate-200"
                      >
                        {presentText(check.rule_code) ?? 'Unnamed rule'}
                      </th>
                      <td className="whitespace-nowrap py-2 pr-3">
                        <CheckOutcome check={check} />
                      </td>
                      <td className="break-all py-2 pr-3 font-mono text-xs tabular-nums text-slate-300">
                        {checkValue(check.threshold, check.unit)}
                      </td>
                      <td className="break-all py-2 font-mono text-xs tabular-nums text-slate-300">
                        {checkValue(check.actual, check.unit)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
