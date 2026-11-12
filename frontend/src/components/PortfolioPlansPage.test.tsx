import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, setToken } from '../api/client';
import { AuthProvider } from '../hooks/useAuth';
import type { AsyncResource } from '../hooks/useAsyncResource';
import type { PlanReview, PlanSummary } from '../types';
import { PortfolioPlansPage } from './PortfolioPlansPage';

const control = vi.hoisted(() => ({
  review: null as AsyncResource<PlanReview> | null,
  list: null as AsyncResource<PlanSummary[]> | null,
  refreshReview: vi.fn(),
  refreshList: vi.fn(),
}));

vi.mock('../hooks/usePortfolioPlans', () => ({
  usePortfolioPlan: () => control.review,
  usePortfolioPlans: () => control.list,
}));

function baseReview(overrides: Partial<PlanReview> = {}): PlanReview {
  const review = {
    plan_id: 'plan-0001',
    portfolio_id: 7,
    status: 'proposed',
    reviewable: true,
    plan_version: 3,
    review_fingerprint: 'fingerprint',
    created_at: '2026-11-12T07:00:00Z',
    updated_at: '2026-11-12T07:00:00Z',
    applied_at: null,
    execution_mode: 'live',
    protocol_version: 'portfolio_allocation.v1',
    policy_version: 'policy.v1',
    proposal_portfolio_version: 'pv-one',
    evaluated_portfolio_version: 'pv-one',
    brief_hash: 'brief-hash',
    approval_hash: 'approval-hash',
    data_sufficiency: 'sufficient',
    objective: 'Reduce concentration.',
    rationale: 'Reduce AAPL and keep cash.',
    actions: [],
    holdings: [
      {
        ticker: 'AAPL',
        quantity: '100',
        avg_cost: '90',
        price: '100',
        price_source: 'test',
        market_value_usd: '10000',
        weight: '0.5',
        sector: 'technology',
        asset_class: 'equity',
        classification_source: 'fixture',
      },
    ],
    weights: [
      {
        ticker: 'AAPL',
        is_cash: false,
        current_weight: '0.5',
        requested_weight: '0.2',
        constrained_weight: '0.2',
        change_from_current: '-0.3',
        requested_vs_constrained: '0',
        executed_weight: null,
        action: 'reduce',
        rationale: 'Reduce concentration.',
        claim_ids: [],
      },
      {
        ticker: 'CASH',
        is_cash: true,
        current_weight: '0.5',
        requested_weight: '0.8',
        constrained_weight: '0.8',
        change_from_current: '0.3',
        requested_vs_constrained: '0',
        executed_weight: null,
        action: 'maintain_cash',
        rationale: 'Cash is deliberate.',
        claim_ids: [],
      },
    ],
    requested_target_weights: { AAPL: '0.2', CASH: '0.8' },
    constrained_target_weights: { AAPL: '0.2', CASH: '0.8' },
    cash_effect: {
      current_cash_weight: '0.5',
      requested_cash_weight: '0.8',
      constrained_cash_weight: '0.8',
      current_cash_usd: '10000',
      target_cash_usd: '16000',
      min_cash_usd: '1000',
      change_usd: '6000',
      buffer_status: 'within_policy',
      note: 'Cash is deliberate.',
    },
    sector_effects: {
      rows: [
        { sector: 'technology', current_weight: '0.5', target_weight: '0.2', change: '-0.3' },
      ],
      asset_class_target: { equity: '0.2' },
      unclassified_target_weight: '0',
      unclassified_target_tickers: [],
      current_coverage: { status: 'complete' },
      target_coverage_status: 'complete',
      note: 'Explicit classifications only.',
    },
    funding: {
      source: 'none',
      starting_cash_usd: '10000',
      sell_proceeds_usd: '6000',
      buy_spend_usd: '0',
      estimated_costs_usd: '6',
      ending_cash_usd: '16000',
      min_cash_usd: '1000',
      assumes_sell_proceeds: false,
      note: 'Sells before buys.',
    },
    costs: {
      estimated_cost_usd: '6',
      estimated_turnover_pct: '30',
      cost_model: { model_version: 'linear_bps.v1' },
      per_leg_estimated_cost_usd: { AAPL: '6' },
      recorded_fee_note: 'Planning estimates only; fees stay NULL.',
      fees_recorded_in_ledger: false,
    },
    estimated_turnover_pct: '30',
    rejected_alternatives: [{ ticker: null, reason: 'No leverage.' }],
    unresolved_uncertainties: ['No expected return was supplied.'],
    deviations: [{ code: 'max_turnover', detail: 'Scaled to the turnover ceiling.' }],
    constraints: { method_version: 'deterministic_constrained_projection.v1' },
    data_quality: {
      limitations: ['No price history was supplied, so correlation is not available.'],
      declared_policies: [{ flag: 'missing_correlation_history', action: 'label_only' }],
      risk_statistics: {
        status: 'not_available',
        method_version: 'covariance.sample_daily_returns.v1',
        history_source: 'none',
        observation_window: null,
        min_observations: 20,
        missing_pairs: [],
        quality_flags: ['no_history'],
        annualisation: null,
        note: 'No correlation was invented.',
      },
      sector_coverage: { status: 'complete' },
      source: { prices: 'input_snapshot', sector: 'explicit_portfolio_classification_lookup' },
    },
    limits: { max_name_weight: '0.3' },
    legs: [
      {
        leg_id: 1,
        sequence: 1,
        ticker: 'AAPL',
        side: 'SELL',
        quantity: '60',
        price: '100',
        notional_usd: '6000',
        estimated_cost_usd: '6',
        priority: 'risk_reducing',
        sector: 'technology',
        asset_class: 'equity',
        status: 'planned',
        transaction_id: null,
        applied_at: null,
        reason: 'Reduce AAPL.',
        signed_cash_effect_usd: '5994',
      },
    ],
    provenance: {
      provider: 'stub',
      model: 'stub-model',
      mode: 'stub',
      synthetic_response: true,
      label: 'Stub decision (no model call)',
      prompt_template_version: 'portfolio_targets.v1',
      protocol_version: 'portfolio_allocation.v1',
      prompt_hash: 'prompt-hash',
      parameters: {},
      input_tokens: 0,
      output_tokens: 0,
      cost_usd: null,
      cost_reported: false,
      token_accounting: 'estimated_chars_per_4_tokens',
      adapter: null,
      calls: [],
      note: 'Stub.',
    },
    submission: {
      run_id: 'run-1',
      mode: 'stub',
      status: 'completed',
      synthetic_response: true,
      provider: 'stub',
      model: 'stub-model',
      request_hash: 'request-hash',
    },
    approval: {
      state: 'pending',
      executable: false,
      required: true,
      plan_version: 3,
      approved_version: null,
      approval_hash: 'approval-hash',
      approved_at: null,
      approved_by: null,
      approved_by_id: null,
      expires_at: null,
      seconds_remaining: null,
      decided_at: null,
      note: '',
      reason: 'No approval has been given for this plan version.',
    },
    approval_history: [],
    execution: {
      status: 'proposed',
      applied_at: null,
      applied_by: null,
      applied_approval_version: null,
      executed_target_weights: {},
      reasons: [],
      transaction_ids: [],
      failure_code: null,
      failure_detail: null,
    },
    revalidation: { required: false, count: 0, revalidated_at: null },
    review_actions: {
      can_approve: true,
      can_reject: true,
      can_cancel: true,
      can_apply: false,
      can_revalidate: true,
      default_apply_mode: 'live',
    },
    immutability: {
      plan_version: 3,
      approval_version: null,
      approval_required: true,
      requires_expiry: true,
      note: 'Approval names this version.',
    },
  } as unknown as PlanReview;
  return { ...review, ...overrides };
}

function resource<T>(data: T | null): AsyncResource<T> {
  return {
    data,
    loading: false,
    error: null,
    refresh: control.refreshReview,
    setData: vi.fn(),
  };
}

function renderPlan(review: PlanReview) {
  control.review = resource(review);
  return render(
    <AuthProvider>
      <PortfolioPlansPage planId={review.plan_id} />
    </AuthProvider>,
  );
}

beforeEach(() => {
  setToken('test-token');
  window.history.replaceState(null, '', '#/plans/plan-0001');
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('PortfolioPlansPage review surface (FIX-08)', () => {
  it('labels a stub honestly and shows requested, constrained, cash, sector and limitations', () => {
    renderPlan(baseReview());

    expect(screen.getByText('Stub decision — not a real model decision')).toBeInTheDocument();
    expect(screen.getAllByText('Stub — no model call').length).toBeGreaterThan(0);
    expect(screen.getByRole('columnheader', { name: 'Requested' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Constrained' })).toBeInTheDocument();
    expect(screen.getAllByText('20.00%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('technology').length).toBeGreaterThan(0);
    expect(screen.getByText(/No price history was supplied/)).toBeInTheDocument();
    expect(screen.getByText(/Planning estimates only/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply approved plan' })).toBeDisabled();
  });

  it('sends the exact plan version and hash when approving, and never applies implicitly', async () => {
    const user = userEvent.setup();
    const approve = vi.spyOn(api, 'approvePlan').mockResolvedValue({
      approval: {
        approval_id: 1,
        decision: 'approved',
        plan_version: 3,
        approval_hash: 'approval-hash',
        decided_at: '2026-11-12T07:05:00Z',
        decided_by: 'owner',
        expires_at: '2026-11-13T07:05:00Z',
        note: 'Looks acceptable.',
      },
      review: baseReview({
        approval: {
          ...baseReview().approval,
          state: 'approved',
          executable: true,
          approved_version: 3,
          approved_by: 'owner',
          approved_at: '2026-11-12T07:05:00Z',
          expires_at: '2026-11-13T07:05:00Z',
          note: 'Looks acceptable.',
          reason: 'The plan is approved for this version.',
        },
        review_actions: {
          ...baseReview().review_actions,
          can_approve: false,
          can_apply: true,
        },
      }),
    });
    const apply = vi.spyOn(api, 'applyPlan');

    renderPlan(baseReview());
    await user.type(screen.getByLabelText('Approval note'), 'Looks acceptable.');
    await user.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
    const call = approve.mock.calls[0];
    expect(call).toBeDefined();
    const body = call![1];
    expect(body.plan_version).toBe(3);
    expect(body.approval_hash).toBe('approval-hash');
    expect(body.note).toBe('Looks acceptable.');
    expect(body.expires_at).toBeTruthy();
    expect(Number.isNaN(Date.parse(body.expires_at ?? ''))).toBe(false);
    expect(apply).not.toHaveBeenCalled();

    expect(apply).not.toHaveBeenCalled();
  });

  it('applies only after an explicit confirmation and sends the selected mode', async () => {
    const user = userEvent.setup();
    const approved = baseReview({
      approval: {
        ...baseReview().approval,
        state: 'approved',
        executable: true,
        approved_version: 3,
        approved_by: 'owner',
        approved_at: '2026-11-12T07:05:00Z',
        expires_at: '2026-11-13T07:05:00Z',
        reason: 'The plan is approved for this version.',
      },
      review_actions: {
        ...baseReview().review_actions,
        can_approve: false,
        can_apply: true,
      },
    });
    const apply = vi.spyOn(api, 'applyPlan').mockResolvedValue({
      application: {
        plan_id: approved.plan_id,
        status: 'applied',
        already_applied: false,
        mode: 'live',
        transaction_ids: [9],
        legs: [],
      },
      input: { mode: 'live' },
      review: { ...approved, status: 'applied' },
    });

    renderPlan(approved);
    await user.click(screen.getByRole('button', { name: 'Apply approved plan' }));
    expect(apply).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Confirm apply' }));
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));
    expect(apply.mock.calls[0]![1]).toBe('live');
  });

  it('requires a rejection reason and records it with the reviewed version', async () => {
    const user = userEvent.setup();
    const reject = vi.spyOn(api, 'rejectPlan').mockResolvedValue({
      approval: {
        approval_id: 2,
        decision: 'rejected',
        plan_version: 3,
        approval_hash: 'approval-hash',
        decided_at: '2026-11-12T07:06:00Z',
        decided_by: 'owner',
        expires_at: null,
        note: 'The data is too thin.',
      },
      review: baseReview({
        status: 'rejected',
        approval: {
          ...baseReview().approval,
          state: 'rejected',
          reason: 'A reviewer rejected this plan.',
        },
        review_actions: {
          ...baseReview().review_actions,
          can_approve: false,
          can_reject: false,
          can_cancel: false,
          can_apply: false,
          can_revalidate: false,
        },
      }),
    });

    renderPlan(baseReview());
    const rejectButton = screen.getByRole('button', { name: 'Reject' });
    expect(rejectButton).toBeDisabled();
    await user.type(screen.getByLabelText('Rejection reason'), 'The data is too thin.');
    await user.click(rejectButton);

    await waitFor(() => expect(reject).toHaveBeenCalledTimes(1));
    const rejectCall = reject.mock.calls[0];
    expect(rejectCall).toBeDefined();
    expect(rejectCall![1]).toEqual({
      reason: 'The data is too thin.',
      plan_version: 3,
    });
  });

  it('shows a live provider label for a real model decision', () => {
    renderPlan(
      baseReview({
        provenance: {
          ...baseReview().provenance,
          provider: 'anthropic',
          model: 'claude-haiku-5-5',
          mode: 'live',
          synthetic_response: false,
          label: 'Live model decision (anthropic/claude-haiku-5-5)',
        },
        submission: {
          ...baseReview().submission!,
          mode: 'live',
          synthetic_response: false,
          provider: 'anthropic',
          model: 'claude-haiku-5-5',
        },
      }),
    );
    expect(screen.getByText('Model decision provenance')).toBeInTheDocument();
    expect(screen.queryByText('Stub decision — not a real model decision')).not.toBeInTheDocument();
    expect(screen.getByText(/Live model decision/)).toBeInTheDocument();
  });
});
