import { useState, type ReactElement, type ReactNode } from 'react';
import { api, errorMessage } from '../api/client';
import { cn } from '../lib/cn';
import { formatCurrency, formatQuantity, formatRelativeTime } from '../lib/format';
import {
  approvalExpiryIso,
  approvalStateLabel,
  approvalStateTone,
  formatPlanWeight,
  formatWeightDelta,
  isSyntheticPlan,
  planProviderLabel,
  planStatusLabel,
  planStatusTone,
} from '../lib/plans';
import { useAuth } from '../hooks/useAuth';
import { useNow } from '../hooks/useNow';
import { usePortfolioPlan, usePortfolioPlans } from '../hooks/usePortfolioPlans';
import type {
  PlanApprovalHistoryRow,
  PlanLegView,
  PlanReview,
  PlanSummary,
  PlanWeightsRow,
} from '../types';
import { Badge, type BadgeTone } from './Badge';
import { EmptyState, ErrorState, SkeletonRows, Spinner } from './Feedback';
import { Panel } from './Panel';

/**
 * Route entry point for the FIX-08 advisory-plan review and approval workflow.
 *
 * Reached at `#/plans` and `#/plans/<id>`, mirroring the Phase 1 decision-run
 * screen. The dark palette and panel primitives are the existing ones; the app
 * layout is not restructured.
 *
 * Two rules are visible rather than implied:
 *
 * * a stub plan is labelled "Stub (no model call)" wherever its provider is
 *   shown, and the review cannot present it as a real model decision;
 * * approval names the exact plan version and an expiry. Applying is a separate
 *   button, disabled unless the current version is approved.
 */

const CONTROL_CLASS =
  'inline-flex min-h-[36px] items-center rounded-md border border-slate-700 bg-slate-950/60 px-2.5 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-200 transition-colors hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950 disabled:cursor-not-allowed disabled:opacity-50';

const PRIMARY_CLASS =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-slate-950 transition-colors hover:bg-emerald-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300 disabled:cursor-not-allowed disabled:opacity-50';

const DANGER_CLASS =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md border border-rose-500/40 px-3 py-1.5 text-xs font-semibold text-rose-300 transition-colors hover:bg-rose-500/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/70 disabled:cursor-not-allowed disabled:opacity-50';

const LINK_CLASS =
  'rounded text-cyan-300 underline decoration-cyan-500/40 underline-offset-2 transition-colors hover:text-cyan-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950';

const INPUT_CLASS =
  'min-h-[32px] w-full rounded-md border border-slate-700 bg-slate-950/70 px-2 py-1 font-mono text-xs text-slate-100 placeholder:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60';

const TABLE_CELL = 'px-2 py-1.5 text-left align-top font-mono text-[11px] tabular-nums';
const TABLE_HEAD = 'px-2 py-1.5 text-left font-mono text-[10px] uppercase tracking-wider text-slate-500';

function DecisionBadge({ decision }: { decision: string }): ReactElement {
  const tone: BadgeTone = decision === 'approved' ? 'emerald' : decision === 'rejected' ? 'rose' : 'slate';
  return <Badge tone={tone}>{decision}</Badge>;
}

function WeightRow({ row }: { row: PlanWeightsRow }): ReactElement {
  const delta = row.change_from_current;
  const deltaNumber = delta === null ? null : Number(delta);
  return (
    <tr className="border-t border-slate-800/60">
      <th scope="row" className={cn(TABLE_CELL, 'font-semibold text-slate-100')}>
        {row.ticker}
        {row.is_cash ? <span className="ml-1 text-[10px] text-slate-500">cash</span> : null}
      </th>
      <td className={TABLE_CELL}>{formatPlanWeight(row.current_weight)}</td>
      <td className={TABLE_CELL}>{formatPlanWeight(row.requested_weight)}</td>
      <td className={TABLE_CELL}>{formatPlanWeight(row.constrained_weight)}</td>
      <td
        className={cn(
          TABLE_CELL,
          deltaNumber !== null && deltaNumber < 0 ? 'text-rose-300' : '',
          deltaNumber !== null && deltaNumber > 0 ? 'text-emerald-300' : '',
        )}
      >
        {formatWeightDelta(delta)}
      </td>
      <td className={TABLE_CELL}>
        {row.action ? (
          <Badge tone={row.action === 'increase' ? 'emerald' : row.action === 'reduce' || row.action === 'exit' ? 'rose' : 'slate'}>
            {row.action}
          </Badge>
        ) : (
          '—'
        )}
      </td>
    </tr>
  );
}

function LegRow({ leg }: { leg: PlanLegView }): ReactElement {
  return (
    <tr className="border-t border-slate-800/60">
      <td className={TABLE_CELL}>{leg.sequence}</td>
      <td className={cn(TABLE_CELL, 'font-semibold text-slate-100')}>{leg.ticker}</td>
      <td className={TABLE_CELL}>
        <Badge tone={leg.side === 'BUY' ? 'emerald' : 'rose'}>{leg.side}</Badge>
      </td>
      <td className={TABLE_CELL}>{formatQuantity(leg.quantity)}</td>
      <td className={TABLE_CELL}>{formatCurrency(leg.price)}</td>
      <td className={TABLE_CELL}>{formatCurrency(leg.notional_usd)}</td>
      <td className={TABLE_CELL}>{formatCurrency(leg.estimated_cost_usd)}</td>
      <td className={TABLE_CELL}>{leg.priority}</td>
      <td className={TABLE_CELL}>
        <Badge tone={leg.status === 'applied' ? 'emerald' : leg.status === 'failed' ? 'rose' : 'slate'}>
          {leg.status}
        </Badge>
        {leg.transaction_id !== null ? (
          <span className="ml-1 text-[10px] text-slate-500">txn {leg.transaction_id}</span>
        ) : null}
      </td>
    </tr>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }): ReactElement {
  return (
    <section className="rounded-lg border border-slate-800/70 bg-slate-950/30 p-3">
      <h3 className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
        {title}
      </h3>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function DefinitionList({ rows }: { rows: Array<[string, ReactElement | string]> }): ReactElement {
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
      {rows.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="font-mono text-[10px] uppercase tracking-wider text-slate-500">{label}</dt>
          <dd className="break-words font-mono text-[11px] text-slate-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function ApprovalCard({
  review,
  busy,
  now,
  onApprove,
  onReject,
  onCancel,
}: {
  review: PlanReview;
  busy: string | null;
  now: number;
  onApprove: (expiresAt: string, note: string) => Promise<void>;
  onReject: (reason: string) => Promise<void>;
  onCancel: (reason: string) => Promise<void>;
}): ReactElement {
  const [expiryHours, setExpiryHours] = useState(24);
  const [note, setNote] = useState('');
  const [rejectReason, setRejectReason] = useState('');
  const [cancelReason, setCancelReason] = useState('');
  const [confirmCancel, setConfirmCancel] = useState(false);
  const approval = review.approval;
  const canApprove = review.review_actions.can_approve && approval.state !== 'approved';
  const canReject = review.review_actions.can_reject;
  const canCancel = review.review_actions.can_cancel;

  return (
    <Panel
      title="Approval"
      subtitle="Approval names this exact plan version and expires. It does not trade."
      actions={
        <>
          <Badge tone={approvalStateTone(approval.state)}>{approvalStateLabel(approval.state)}</Badge>
          <Badge tone="slate">v{approval.plan_version}</Badge>
        </>
      }
    >
      <p className="text-sm leading-relaxed text-slate-300">{approval.reason}</p>
      <div className="mt-3">
        <DefinitionList
          rows={[
            ['Approved version', approval.approved_version ? `v${approval.approved_version}` : '—'],
            ['Approved by', approval.approved_by ?? '—'],
            ['Approved at', approval.approved_at ? formatRelativeTime(approval.approved_at, now) : '—'],
            ['Expires', approval.expires_at ? `${formatRelativeTime(approval.expires_at, now)}` : '—'],
            ['Permission hash', approval.approval_hash ? approval.approval_hash.slice(0, 18) + '…' : '—'],
            ['Plan version', `v${review.plan_version}`],
          ]}
        />
      </div>
      {approval.note ? (
        <p className="mt-2 rounded border border-slate-800 bg-slate-900/60 px-2 py-1.5 text-xs text-slate-300">
          {approval.note}
        </p>
      ) : null}

      {canApprove ? (
        <div className="mt-3 rounded-lg border border-emerald-500/25 bg-emerald-500/[0.04] p-3">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-emerald-300">
            Approve plan version {review.plan_version}
          </p>
          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end">
            <label className="block text-xs text-slate-400 sm:w-44">
              <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-500">
                Expires in
              </span>
              <select
                aria-label="Approval expiry"
                value={expiryHours}
                onChange={(event) => setExpiryHours(Number(event.target.value))}
                className={INPUT_CLASS}
              >
                <option value={1}>1 hour</option>
                <option value={24}>24 hours</option>
                <option value={168}>7 days</option>
              </select>
            </label>
            <label className="block min-w-0 flex-1 text-xs text-slate-400">
              <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-500">
                Review note (optional)
              </span>
              <input
                aria-label="Approval note"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="Why this allocation is acceptable"
                className={INPUT_CLASS}
              />
            </label>
            <button
              type="button"
              className={PRIMARY_CLASS}
              disabled={busy !== null}
              onClick={() => onApprove(approvalExpiryIso(expiryHours), note)}
            >
              {busy === 'approve' ? <Spinner className="border-slate-900/40 border-t-slate-900" /> : null}
              Approve
            </button>
          </div>
        </div>
      ) : null}

      {canReject ? (
        <div className="mt-3 rounded-lg border border-rose-500/25 bg-rose-500/[0.03] p-3">
          <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-rose-300">
            Reject this plan
          </p>
          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-end">
            <label className="block min-w-0 flex-1 text-xs text-slate-400">
              <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-500">
                Reason (required)
              </span>
              <input
                aria-label="Rejection reason"
                value={rejectReason}
                onChange={(event) => setRejectReason(event.target.value)}
                placeholder="What makes this allocation unacceptable?"
                className={INPUT_CLASS}
              />
            </label>
            <button
              type="button"
              className={DANGER_CLASS}
              disabled={busy !== null || rejectReason.trim().length < 3}
              onClick={() => onReject(rejectReason)}
            >
              {busy === 'reject' ? <Spinner className="border-rose-900/40 border-t-rose-300" /> : null}
              Reject
            </button>
          </div>
        </div>
      ) : null}

      {canCancel ? (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {confirmCancel ? (
            <>
              <input
                aria-label="Cancellation reason"
                value={cancelReason}
                onChange={(event) => setCancelReason(event.target.value)}
                placeholder="Optional reason"
                className={cn(INPUT_CLASS, 'sm:w-72')}
              />
              <button
                type="button"
                className={DANGER_CLASS}
                disabled={busy !== null}
                onClick={() => onCancel(cancelReason)}
              >
                Confirm cancel
              </button>
              <button type="button" className={CONTROL_CLASS} onClick={() => setConfirmCancel(false)}>
                Keep plan
              </button>
            </>
          ) : (
            <button type="button" className={CONTROL_CLASS} onClick={() => setConfirmCancel(true)}>
              Cancel plan
            </button>
          )}
        </div>
      ) : null}

      {review.approval_history.length > 0 ? (
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full">
            <caption className="pb-1 text-left font-mono text-[10px] uppercase tracking-wider text-slate-500">
              Approval audit history
            </caption>
            <thead>
              <tr>
                <th scope="col" className={TABLE_HEAD}>Decision</th>
                <th scope="col" className={TABLE_HEAD}>Version</th>
                <th scope="col" className={TABLE_HEAD}>By</th>
                <th scope="col" className={TABLE_HEAD}>Decided</th>
                <th scope="col" className={TABLE_HEAD}>Expires</th>
                <th scope="col" className={TABLE_HEAD}>Note</th>
              </tr>
            </thead>
            <tbody>
              {review.approval_history.map((row: PlanApprovalHistoryRow) => (
                <tr key={row.approval_id} className="border-t border-slate-800/60">
                  <td className={TABLE_CELL}><DecisionBadge decision={row.decision} /></td>
                  <td className={TABLE_CELL}>v{row.plan_version}</td>
                  <td className={TABLE_CELL}>{row.decided_by ?? '—'}</td>
                  <td className={TABLE_CELL}>{row.decided_at ? formatRelativeTime(row.decided_at, now) : '—'}</td>
                  <td className={TABLE_CELL}>{row.expires_at ? formatRelativeTime(row.expires_at, now) : '—'}</td>
                  <td className={cn(TABLE_CELL, 'max-w-[28rem] break-words font-sans text-[11px] text-slate-400')}>
                    {row.note || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Panel>
  );
}

function PlanReviewView({ planId }: { planId: string }): ReactElement {
  const { token } = useAuth();
  const review = usePortfolioPlan(planId, token);
  const now = useNow(15_000);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [applyMode, setApplyMode] = useState<'live' | 'paper'>('live');
  const [confirmApply, setConfirmApply] = useState(false);

  const data = review.data;
  const handleError = (cause: unknown): void => {
    setActionError(errorMessage(cause));
    setNotice(null);
  };

  const runAction = async (label: string, action: () => Promise<PlanReview>): Promise<void> => {
    setBusy(label);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await action();
      review.setData(() => updated);
      setNotice(`${label} recorded. No trade is placed unless you press Apply.`);
    } catch (cause) {
      handleError(cause);
    } finally {
      setBusy(null);
    }
  };

  const onApprove = async (expiresAt: string, note: string): Promise<void> => {
    if (!data) return;
    await runAction('approve', () =>
      api
        .approvePlan(data.plan_id, {
          plan_version: data.plan_version,
          approval_hash: data.approval_hash,
          expires_at: expiresAt,
          note,
        })
        .then((response) => response.review),
    );
  };
  const onReject = async (reason: string): Promise<void> => {
    if (!data) return;
    await runAction('reject', () =>
      api
        .rejectPlan(data.plan_id, { reason, plan_version: data.plan_version })
        .then((response) => response.review),
    );
  };
  const onCancel = async (reason: string): Promise<void> => {
    if (!data) return;
    await runAction('cancel', () =>
      api.cancelPlan(data.plan_id, { reason }).then((response) => response.review),
    );
  };
  const onRevalidate = async (): Promise<void> => {
    if (!data) return;
    await runAction('revalidate', () =>
      api.revalidatePlan(data.plan_id).then((response) => response.review),
    );
  };
  const onApply = async (): Promise<void> => {
    if (!data) return;
    setBusy('apply');
    setActionError(null);
    setNotice(null);
    try {
      const response = await api.applyPlan(data.plan_id, applyMode);
      review.setData(() => response.review);
      setNotice(
        response.application.already_applied
          ? 'This plan was already applied; no second ledger row was written.'
          : `Applied through the shared execution service (${response.application.mode}).`,
      );
    } catch (cause) {
      handleError(cause);
    } finally {
      setBusy(null);
      setConfirmApply(false);
    }
  };

  if (review.loading && !data) {
    return <SkeletonRows rows={6} />;
  }
  if (review.error && !data) {
    return <ErrorState message={`Could not load this plan — ${review.error}`} onRetry={review.refresh} />;
  }
  if (!data) {
    return <EmptyState title="Plan not found" description="The plan id does not resolve to a plan you can review." />;
  }

  const synthetic = isSyntheticPlan(data);
  const canApply = data.review_actions.can_apply && data.status === 'proposed';
  const funding = data.funding ?? {};
  const sectorRows = data.sector_effects.rows;

  return (
    <div className="space-y-3">
      <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-2 text-sm">
        <a href="#/plans" className={LINK_CLASS}>← All portfolio plans</a>
        <span aria-hidden="true" className="text-slate-600">/</span>
        <span className="break-all font-mono text-xs text-slate-400">{data.plan_id}</span>
      </nav>

      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={planStatusTone(data.status)}>{planStatusLabel(data.status)}</Badge>
        <Badge tone={approvalStateTone(data.approval.state)}>{approvalStateLabel(data.approval.state)}</Badge>
        <Badge tone="slate">v{data.plan_version}</Badge>
        <Badge tone={synthetic ? 'violet' : 'emerald'}>
          {synthetic ? 'Stub — no model call' : planProviderLabel(data.provenance)}
        </Badge>
        <span className="ml-auto font-mono text-[10px] text-slate-500">
          {data.created_at ? `Created ${formatRelativeTime(data.created_at, now)}` : 'Creation time unknown'}
        </span>
      </div>

      <div
        data-state={synthetic ? 'plan-stub-provenance' : 'plan-live-provenance'}
        className={cn(
          'rounded-xl border px-4 py-3',
          synthetic
            ? 'border-violet-500/30 bg-violet-500/[0.06]'
            : 'border-emerald-500/30 bg-emerald-500/[0.05]',
        )}
      >
        <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-300">
          {synthetic ? 'Stub decision — not a real model decision' : 'Model decision provenance'}
        </p>
        <p className="mt-1 text-sm leading-relaxed text-slate-300">
          {synthetic
            ? 'This plan was produced by an explicitly selected stub. No provider was called and it must not be presented as a real model decision.'
            : `${data.provenance.label}. Prompt template ${data.provenance.prompt_template_version ?? 'unknown'}, ${data.provenance.input_tokens ?? '—'} input / ${data.provenance.output_tokens ?? '—'} output tokens (${data.provenance.token_accounting ?? 'token basis unknown'}).`}
        </p>
        {data.provenance.cost_reported ? null : (
          <p className="mt-1 text-xs text-amber-200/80">
            Cost is an estimate; the provider did not report a billed dollar amount.
          </p>
        )}
      </div>

      {actionError ? <ErrorState message={actionError} onRetry={review.refresh} /> : null}
      {notice ? (
        <div role="status" className="rounded-lg border border-sky-500/30 bg-sky-500/[0.06] px-3 py-2 text-sm text-sky-200">
          {notice}
        </div>
      ) : null}

      <ApprovalCard
        review={data}
        busy={busy}
        now={now}
        onApprove={onApprove}
        onReject={onReject}
        onCancel={onCancel}
      />

      <Panel
        title="Requested vs constrained allocation"
        subtitle="The model requests weights; the deterministic projection decides what the portfolio can support."
        actions={
          <button type="button" onClick={review.refresh} disabled={review.loading} className={CONTROL_CLASS}>
            Refresh
          </button>
        }
        bodyClassName="p-3 sm:p-4"
      >
        <p className="text-sm leading-relaxed text-slate-300">{data.rationale || data.objective || 'No rationale recorded.'}</p>
        <div className="mt-3 overflow-x-auto">
          <table className="min-w-full">
            <caption className="pb-1 text-left font-mono text-[10px] uppercase tracking-wider text-slate-500">
              Weights as fractions of equity; the difference column is constrained minus current.
            </caption>
            <thead>
              <tr>
                <th scope="col" className={TABLE_HEAD}>Ticker</th>
                <th scope="col" className={TABLE_HEAD}>Current</th>
                <th scope="col" className={TABLE_HEAD}>Requested</th>
                <th scope="col" className={TABLE_HEAD}>Constrained</th>
                <th scope="col" className={TABLE_HEAD}>Change</th>
                <th scope="col" className={TABLE_HEAD}>Action</th>
              </tr>
            </thead>
            <tbody>
              {data.weights.map((row) => (
                <WeightRow key={row.ticker} row={row} />
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 font-mono text-[10px] text-slate-500">
          Data sufficiency: {data.data_sufficiency ?? 'not stated'} · turnover{' '}
          {data.estimated_turnover_pct ? `${Number(data.estimated_turnover_pct).toFixed(2)}%` : '—'}
        </p>
      </Panel>

      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Holdings" bodyClassName="p-3">
          <div className="overflow-x-auto">
            <table className="min-w-full">
              <thead>
                <tr>
                  <th scope="col" className={TABLE_HEAD}>Ticker</th>
                  <th scope="col" className={TABLE_HEAD}>Quantity</th>
                  <th scope="col" className={TABLE_HEAD}>Mark</th>
                  <th scope="col" className={TABLE_HEAD}>Value</th>
                  <th scope="col" className={TABLE_HEAD}>Sector</th>
                </tr>
              </thead>
              <tbody>
                {data.holdings.map((holding) => (
                  <tr key={holding.ticker} className="border-t border-slate-800/60">
                    <th scope="row" className={cn(TABLE_CELL, 'font-semibold text-slate-100')}>{holding.ticker}</th>
                    <td className={TABLE_CELL}>{formatQuantity(holding.quantity)}</td>
                    <td className={TABLE_CELL}>
                      {formatCurrency(holding.price)}
                      <span className="ml-1 text-[10px] text-slate-500">{holding.price_source ?? ''}</span>
                    </td>
                    <td className={TABLE_CELL}>{formatCurrency(holding.market_value_usd)}</td>
                    <td className={TABLE_CELL}>{holding.sector ?? <span className="text-amber-300">unclassified</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            A missing sector is unknown, not a default bucket. No sector was inferred from a ticker.
          </p>
        </Panel>

        <Panel title="Cash and sector effects" bodyClassName="p-3">
          <DefinitionList
            rows={[
              ['Current cash', formatPlanWeight(data.cash_effect.current_cash_weight)],
              ['Requested cash', formatPlanWeight(data.cash_effect.requested_cash_weight)],
              ['Constrained cash', formatPlanWeight(data.cash_effect.constrained_cash_weight)],
              ['Current cash USD', formatCurrency(data.cash_effect.current_cash_usd)],
              ['Target cash USD', formatCurrency(data.cash_effect.target_cash_usd)],
              ['Minimum cash USD', formatCurrency(data.cash_effect.min_cash_usd)],
              ['Cash buffer', data.cash_effect.buffer_status.replace(/_/g, ' ')],
              ['Sector coverage', data.sector_effects.target_coverage_status],
            ]}
          />
          <div className="mt-3 overflow-x-auto">
            <table className="min-w-full">
              <caption className="pb-1 text-left font-mono text-[10px] uppercase tracking-wider text-slate-500">
                Sector exposure now and after the constrained plan
              </caption>
              <thead>
                <tr>
                  <th scope="col" className={TABLE_HEAD}>Sector</th>
                  <th scope="col" className={TABLE_HEAD}>Current</th>
                  <th scope="col" className={TABLE_HEAD}>Target</th>
                  <th scope="col" className={TABLE_HEAD}>Change</th>
                </tr>
              </thead>
              <tbody>
                {sectorRows.map((row) => (
                  <tr key={row.sector} className="border-t border-slate-800/60">
                    <th scope="row" className={cn(TABLE_CELL, 'font-semibold text-slate-100')}>{row.sector}</th>
                    <td className={TABLE_CELL}>{formatPlanWeight(row.current_weight)}</td>
                    <td className={TABLE_CELL}>{formatPlanWeight(row.target_weight)}</td>
                    <td className={TABLE_CELL}>{formatWeightDelta(row.change)}</td>
                  </tr>
                ))}
                <tr className="border-t border-slate-800/60">
                  <th scope="row" className={cn(TABLE_CELL, 'font-semibold text-slate-100')}>Unclassified</th>
                  <td className={TABLE_CELL}>—</td>
                  <td className={TABLE_CELL}>{formatPlanWeight(data.sector_effects.unclassified_target_weight)}</td>
                  <td className={TABLE_CELL}>—</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">{data.sector_effects.note}</p>
        </Panel>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Funding and costs" bodyClassName="p-3">
          <DefinitionList
            rows={[
              ['Funding source', String(funding.source ?? data.provenance.mode)],
              ['Starting cash', formatCurrency(funding.starting_cash_usd as string | undefined)],
              ['Sell proceeds', formatCurrency(funding.sell_proceeds_usd as string | undefined)],
              ['Buy spend', formatCurrency(funding.buy_spend_usd as string | undefined)],
              ['Estimated costs', formatCurrency(funding.estimated_costs_usd as string | undefined)],
              ['Ending cash', formatCurrency(funding.ending_cash_usd as string | undefined)],
              ['Estimated plan cost', formatCurrency(data.costs.estimated_cost_usd)],
              ['Estimated turnover', data.costs.estimated_turnover_pct ? `${Number(data.costs.estimated_turnover_pct).toFixed(2)}%` : '—'],
            ]}
          />
          <p className="mt-2 text-xs leading-relaxed text-amber-200/80">{data.costs.recorded_fee_note}</p>
          {data.costs.per_leg_estimated_cost_usd ? (
            <p className="mt-1 font-mono text-[10px] text-slate-500">
              Per-leg estimates:{' '}
              {Object.entries(data.costs.per_leg_estimated_cost_usd)
                .map(([ticker, cost]) => `${ticker} ${formatCurrency(cost)}`)
                .join(' · ') || 'none'}
            </p>
          ) : null}
          <p className="mt-1 text-xs leading-relaxed text-slate-500">
            {String(funding.note ?? '')}
          </p>
        </Panel>

        <Panel title="Rejected alternatives and uncertainties" bodyClassName="p-3">
          <ul className="space-y-1.5">
            {data.rejected_alternatives.length === 0 ? (
              <li className="text-xs text-slate-500">No alternatives were recorded.</li>
            ) : (
              data.rejected_alternatives.map((row, index) => (
                <li key={`${row.ticker ?? 'portfolio'}-${index}`} className="text-xs leading-relaxed text-slate-300">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
                    {row.ticker ?? 'portfolio'}
                  </span>{' '}
                  {row.reason}
                </li>
              ))
            )}
          </ul>
          <div className="mt-3 border-t border-slate-800/70 pt-2">
            <p className="font-mono text-[10px] uppercase tracking-wider text-slate-500">Unresolved uncertainties</p>
            <ul className="mt-1 space-y-1">
              {data.unresolved_uncertainties.length === 0 ? (
                <li className="text-xs text-slate-500">None recorded.</li>
              ) : (
                data.unresolved_uncertainties.map((text, index) => (
                  <li key={index} className="text-xs leading-relaxed text-slate-400">• {text}</li>
                ))
              )}
            </ul>
          </div>
        </Panel>
      </div>

      <Panel title="Data-quality limitations" bodyClassName="p-3">
        <div className="grid gap-3 lg:grid-cols-2">
          <Block title="Limitations">
            <ul className="space-y-1.5">
              {data.data_quality.limitations && data.data_quality.limitations.length > 0 ? (
                data.data_quality.limitations.map((text, index) => (
                  <li key={index} className="text-xs leading-relaxed text-slate-400">• {text}</li>
                ))
              ) : (
                <li className="text-xs text-slate-500">No limitation was recorded.</li>
              )}
            </ul>
          </Block>
          <Block title="Declared policies and risk statistics">
            <DefinitionList
              rows={[
                ['Correlation status', data.data_quality.risk_statistics?.status ?? '—'],
                ['History source', data.data_quality.risk_statistics?.history_source ?? '—'],
                ['Minimum observations', String(data.data_quality.risk_statistics?.min_observations ?? '—')],
                ['Sector coverage', String((data.data_quality.sector_coverage as { status?: string } | undefined)?.status ?? data.sector_effects.target_coverage_status)],
                ['Price source', data.data_quality.source?.prices ?? '—'],
                ['Sector source', data.data_quality.source?.sector ?? '—'],
              ]}
            />
            {data.data_quality.declared_policies && data.data_quality.declared_policies.length > 0 ? (
              <ul className="mt-2 space-y-1">
                {data.data_quality.declared_policies.map((policy, index) => (
                  <li key={index} className="font-mono text-[10px] text-amber-200/80">
                    {JSON.stringify(policy)}
                  </li>
                ))}
              </ul>
            ) : null}
          </Block>
        </div>
        <details className="mt-2 rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5">
          <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wider text-slate-500">
            Deviations ({data.deviations.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {data.deviations.map((row, index) => (
              <li key={`${row.code}-${index}`} className="text-xs leading-relaxed text-slate-400">
                <span className="font-mono text-[10px] text-amber-300">{row.code}</span>{' '}
                {typeof row.detail === 'string' ? row.detail : ''}
              </li>
            ))}
          </ul>
        </details>
      </Panel>

      <Panel
        title="Execution"
        subtitle="Legs are only written by the shared execution service, after an explicit apply."
        actions={<Badge tone={planStatusTone(data.execution.status)}>{planStatusLabel(data.execution.status)}</Badge>}
        bodyClassName="p-3"
      >
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <label className="block text-xs text-slate-400">
            <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-slate-500">
              Apply mode
            </span>
            <select
              aria-label="Apply mode"
              value={applyMode}
              onChange={(event) => setApplyMode(event.target.value === 'paper' ? 'paper' : 'live')}
              className={INPUT_CLASS}
            >
              <option value="live">live — current quotes required</option>
              <option value="paper">paper — plan marks, labelled</option>
            </select>
          </label>
          {confirmApply ? (
            <>
              <button type="button" className={PRIMARY_CLASS} disabled={busy !== null} onClick={onApply}>
                {busy === 'apply' ? <Spinner className="border-slate-900/40 border-t-slate-900" /> : null}
                Confirm apply
              </button>
              <button type="button" className={CONTROL_CLASS} onClick={() => setConfirmApply(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className={PRIMARY_CLASS}
              disabled={busy !== null || !canApply}
              onClick={() => setConfirmApply(true)}
              title={canApply ? undefined : 'A current, unexpired approval for this version is required.'}
            >
              Apply approved plan
            </button>
          )}
          {data.review_actions.can_revalidate ? (
            <button type="button" className={CONTROL_CLASS} disabled={busy !== null} onClick={onRevalidate}>
              Revalidate
            </button>
          ) : null}
          {!canApply && data.status === 'proposed' ? (
            <span className="text-xs text-slate-500">
              Execution is disabled until the current version is approved and unexpired.
            </span>
          ) : null}
        </div>
        {data.execution.failure_detail ? (
          <p className="mb-2 rounded border border-rose-500/30 bg-rose-500/5 px-2 py-1.5 text-xs text-rose-300">
            {data.execution.failure_code}: {data.execution.failure_detail}
          </p>
        ) : null}
        <div className="overflow-x-auto">
          <table className="min-w-full">
            <thead>
              <tr>
                <th scope="col" className={TABLE_HEAD}>#</th>
                <th scope="col" className={TABLE_HEAD}>Ticker</th>
                <th scope="col" className={TABLE_HEAD}>Side</th>
                <th scope="col" className={TABLE_HEAD}>Qty</th>
                <th scope="col" className={TABLE_HEAD}>Price</th>
                <th scope="col" className={TABLE_HEAD}>Notional</th>
                <th scope="col" className={TABLE_HEAD}>Est. cost</th>
                <th scope="col" className={TABLE_HEAD}>Priority</th>
                <th scope="col" className={TABLE_HEAD}>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.legs.length === 0 ? (
                <tr>
                  <td className={TABLE_CELL} colSpan={9}>No legs: this plan proposes no trade.</td>
                </tr>
              ) : (
                data.legs.map((leg) => <LegRow key={leg.leg_id} leg={leg} />)
              )}
            </tbody>
          </table>
        </div>
        {data.execution.applied_at ? (
          <p className="mt-2 font-mono text-[10px] text-slate-500">
            Applied {formatRelativeTime(data.execution.applied_at, now)}
            {data.execution.applied_by ? ` by ${data.execution.applied_by}` : ''}
            {data.execution.applied_approval_version ? ` under approval v${data.execution.applied_approval_version}` : ''}
            {data.execution.transaction_ids.length > 0 ? ` · ledger rows ${data.execution.transaction_ids.join(', ')}` : ''}
          </p>
        ) : null}
      </Panel>
    </div>
  );
}

function PlanRow({ plan }: { plan: PlanSummary }): ReactElement {
  const synthetic = plan.synthetic_response;
  return (
    <li className="min-w-0">
      <a
        href={`#/plans/${encodeURIComponent(plan.plan_id)}`}
        className="block min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/40 px-3 py-2.5 transition-colors hover:border-slate-700 hover:bg-slate-900/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm font-semibold tracking-wide text-slate-100">
            {plan.plan_id.slice(0, 8)}
          </span>
          <Badge tone={planStatusTone(plan.status)}>{planStatusLabel(plan.status)}</Badge>
          <Badge tone={approvalStateTone(plan.approval_state)}>{approvalStateLabel(plan.approval_state)}</Badge>
          <Badge tone={synthetic ? 'violet' : 'emerald'}>
            {synthetic ? 'Stub (no model call)' : planProviderLabel(null, plan)}
          </Badge>
          <span className="ml-auto font-mono text-[11px] text-slate-400">
            {plan.created_at ? formatRelativeTime(plan.created_at) : 'time unknown'}
          </span>
        </div>
        <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-400">
          {plan.objective || 'No objective recorded.'}
        </p>
        <p className="mt-1 flex flex-wrap gap-x-3 font-mono text-[10px] text-slate-500">
          <span>v{plan.plan_version}</span>
          <span>{plan.protocol_version}</span>
          <span>Est. cost {formatCurrency(plan.estimated_cost_usd)}</span>
          {plan.revalidation_required ? <span className="text-amber-300">Revalidation required</span> : null}
        </p>
      </a>
    </li>
  );
}

function PlanList(): ReactElement {
  const { token } = useAuth();
  const plans = usePortfolioPlans(token);
  const rows = plans.data ?? [];

  return (
    <Panel
      title="Advisory portfolio plans"
      subtitle="One row per plan. Open one to review the requested and constrained allocation, its data limitations, and to approve or reject it."
      actions={
        <button type="button" onClick={plans.refresh} disabled={plans.loading} className={CONTROL_CLASS}>
          Refresh
        </button>
      }
      bodyClassName="p-3 sm:p-4"
    >
      {plans.error && rows.length === 0 ? (
        <ErrorState message={`Could not load portfolio plans — ${plans.error}`} onRetry={plans.refresh} />
      ) : plans.loading && rows.length === 0 ? (
        <SkeletonRows rows={5} />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No portfolio plans"
          description="Submit a portfolio analysis to create a proposed plan. Submission is advisory and places no trade."
        />
      ) : (
        <div className="space-y-3">
          {plans.error ? (
            <ErrorState compact message={`Could not refresh — ${plans.error}`} onRetry={plans.refresh} />
          ) : null}
          <ul aria-label="Portfolio plans" className="space-y-2">
            {rows.map((plan) => (
              <PlanRow key={plan.plan_id} plan={plan} />
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

export interface PortfolioPlansPageProps {
  planId: string | null;
}

export function PortfolioPlansPage({ planId }: PortfolioPlansPageProps): ReactElement {
  return (
    <div className="min-h-screen bg-slate-950">
      <header className="border-b border-slate-800/80 bg-slate-900/40 px-3 py-3 sm:px-4">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">
              AlphaAgent · Phase 4 advisory allocation
            </p>
            <h1 className="mt-0.5 text-lg font-semibold tracking-tight text-slate-100">
              Portfolio plan review
            </h1>
          </div>
          <nav aria-label="Primary" className="flex flex-wrap items-center gap-2">
            <a href="#/plans" className={CONTROL_CLASS}>All plans</a>
            <a href="#/runs" className={CONTROL_CLASS}>Decision runs</a>
            <a href="#/" className={CONTROL_CLASS}>Dashboard</a>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-[1500px] space-y-4 px-3 py-4 sm:px-4">
        {planId ? <PlanReviewView key={planId} planId={planId} /> : <PlanList />}
      </main>
    </div>
  );
}
