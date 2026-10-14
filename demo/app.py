"""AlphaAgent offline demo - the project's *real* execution guard, no API keys, no database.

WHY this file is so thin: the safety-critical heart of AlphaAgent
(``services.execution.evaluate_proposal``) is a pure function that performs no
I/O, but it does import Django models. So instead of reimplementing the
guardrails for a demo (which would let the demo drift away from the real
behaviour), the app configures the smallest possible Django settings module at
import time and calls the genuine function - on **unsaved** model instances,
which never touch a database. Only the *inputs* are wired to widgets here.

Every input the guard takes is supplied, including the market values it refuses
to fetch itself (``services.execution.Exposure``). That matters: given no
exposure the guard skips its two portfolio-level ceilings, so a page that omitted
it would be driving only half the guard.

Run locally:  streamlit run demo/app.py
"""

from __future__ import annotations

import inspect
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import django
import streamlit as st
from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_URL = "https://github.com/SergeyGer/AlphaAgent"
GUARD_SOURCE = REPO_ROOT / "services" / "execution.py"

CENT = Decimal("0.01")
PCT_STEP = Decimal("0.01")
PRESETS_PER_ROW = 3

st.set_page_config(
    page_title="AlphaAgent - offline guard demo",
    page_icon="🛡️",
    layout="wide",
)


def _bootstrap_django() -> None:
    """Make ``core.models`` importable without a database, a server or secrets.

    WHY: ``services/execution.py`` imports ``core.models`` at module level, so a
    bare import fails outside a configured Django project. The models only need
    the app registry plus auth/contenttypes for ``AUTH_USER_MODEL``;
    ``DATABASES={}`` is deliberate - if any code path tried to open a connection
    it would fail loudly instead of silently reaching a database. The demo only
    ever builds UNSAVED instances, and a pure function cannot tell the
    difference between a saved and an unsaved row.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if not settings.configured:
        settings.configure(
            INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "core"],
            DATABASES={},
            USE_TZ=True,
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            # Django insists the setting exists, though this demo never signs a
            # cookie or serves a session. No secret is needed or shipped; the env
            # var is only there so the placeholder can be overridden if desired.
            SECRET_KEY=os.environ.get("DJANGO_SECRET_KEY", "alphaagent-offline-demo"),
        )
        django.setup()


if not GUARD_SOURCE.exists():
    st.error(
        "Cannot find `services/execution.py`. This demo must run from inside the "
        "AlphaAgent repository (entrypoint `demo/app.py`); it deliberately does not "
        "carry a private copy of the guard."
    )
    st.stop()

_bootstrap_django()

from core.models import Portfolio  # noqa: E402
from services.execution import Exposure, GuardDecision, evaluate_proposal  # noqa: E402
from services.ledger import PositionState  # noqa: E402


@dataclass(frozen=True)
class Proposal:
    """Duck-typed stand-in for ``ai_agent.TradeProposal``.

    WHY a stand-in: ``evaluate_proposal`` reads exactly two attributes of the
    proposal - ``action`` and ``amount``. Importing the real Pydantic model
    would drag CrewAI, requests and defusedxml into this demo's dependency set
    for zero behavioural gain; the guard under test here is the unmodified
    project code.
    """

    action: str
    amount: float


@dataclass(frozen=True)
class Preset:
    """One-click scenario, so a visitor sees the interesting guard paths immediately."""

    label: str
    expectation: str
    values: dict[str, Any]


# Every preset states the same four portfolio facts (cash, what else is held,
# what this ticker is worth via units held x price) plus the two ceilings, so each
# one shows a single interesting guard path rather than a combination.
PRESETS: tuple[Preset, ...] = (
    Preset(
        label="1 - Normal trade (allowed)",
        expectation=(
            "3 units @ $100 = $300 against a $500 per-trade budget: approved untouched. "
            "$11,000 of equity, so neither portfolio ceiling bites either."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="2 - Breaches the allocation ceiling (clamped)",
        expectation=(
            "$5,000 order against a $500 per-trade ceiling: the guard sizes it down to $500, "
            "and the note names the ceiling that did it."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 50.0,
            "position": 10.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="3 - Ceiling leaves $0 budget (rejected)",
        expectation=(
            "max_trade_allocation_pct = 0%: no trade can fit, so every BUY is refused - "
            "the guard never reaches the portfolio-level limits."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 0.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="4 - Daily loss limit already hit (rejected)",
        expectation=(
            "-$600 realised against a $500 kill switch: new risk is frozen, while the "
            "mirrored SELL below still goes through."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": -600.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="5 - Oversized SELL (clamped to position)",
        expectation=(
            "Selling 50 units while holding 10: clamped to the position, never short. "
            "Neither portfolio ceiling applies to de-risking."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "SELL",
            "amount": 50.0,
            "position": 10.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="6 - Exposure ceiling clamps the buy",
        expectation=(
            "$88,000 invested of $98,000 equity = 89.8% against a 90% ceiling, so $200 of "
            "headroom is left: the $300 buy is clamped to 2 units and the note names the "
            "exposure ceiling, not the $500 per-trade budget."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
            "other_positions": 87_000.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="7 - Concentration clamps the buy",
        expectation=(
            "40 units @ $100 = $4,000 of $14,000 equity against a 30% single-instrument "
            "ceiling, so $200 of headroom is left: the $300 buy is clamped to 2 units - "
            "a different ceiling from preset 6, same clamp behaviour."
        ),
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 40.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="8 - Both ceilings bind (smaller wins)",
        expectation=(
            "Exposure allows $100 more, concentration only $50, the per-trade budget $125: "
            "the smallest headroom wins, so the buy is clamped to 1 unit ($50) and the note "
            "names concentration."
        ),
        values={
            "cash": 500.0,
            "allocation": 25.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 50.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 23.0,
            "other_positions": 2_350.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
    Preset(
        label="9 - Concentration full (rejected)",
        expectation=(
            "30 units @ $100 = exactly 30% of $10,000 equity: zero concentration headroom, "
            "so the buy has nothing to be sized down to and is refused. A clamp says which "
            "ceiling bound it; a refusal is the guard's shared 'cannot be sized down' message."
        ),
        values={
            "cash": 7_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 30.0,
            "other_positions": 0.0,
            "exposure_pct": 90.0,
            "concentration_pct": 30.0,
        },
    ),
)


# Initial state, identical to preset 1. WHY a dict instead of each widget's
# ``value=``: a widget given both a ``value`` argument and a session-state entry
# makes Streamlit log "created with a default value but also had its value set via
# the Session State API" on every preset click.
WIDGET_DEFAULTS: dict[str, Any] = {
    "cash": 10_000.0,
    "allocation": 5.0,
    "loss_limit": 500.0,
    "realised": 0.0,
    "ticker": "AAPL",
    "side": "BUY",
    "amount": 3.0,
    "price": 100.0,
    "position": 10.0,
    "other_positions": 0.0,
    "exposure_pct": 90.0,
    "concentration_pct": 30.0,
}


@dataclass(frozen=True)
class GuardInputs:
    """What the playground feeds the guard, already in the units the ORM would store."""

    cash: Decimal
    allocation_pct: Decimal
    loss_limit: Decimal
    realised_pnl: Decimal
    price: Decimal
    side: str
    amount: Decimal
    position_amount: Decimal
    ticker: str
    other_positions_value: Decimal
    exposure_pct: Decimal
    concentration_pct: Decimal

    # -- The three quantities ``Exposure`` carries -------------------------
    # Derived rather than asked for twice, so the page cannot be made to show a
    # portfolio that does not add up. This mirrors how the production callers
    # build the same object in ``services.portfolio_metrics.build_exposure``:
    # every holding is marked at market, and equity is cash plus those holdings.
    @property
    def ticker_value_usd(self) -> Decimal:
        """This instrument's holding, at the same price the guard trades at."""
        return (self.position_amount * self.price).quantize(CENT)

    @property
    def positions_value_usd(self) -> Decimal:
        """Every holding at market, this instrument included."""
        return (self.other_positions_value + self.ticker_value_usd).quantize(CENT)

    @property
    def equity_usd(self) -> Decimal:
        """Cash plus positions - the denominator for both portfolio ceilings."""
        return (self.cash + self.positions_value_usd).quantize(CENT)


def _dec(value: float | str) -> Decimal:
    """Convert a widget float via ``str`` so 0.1 does not arrive as 0.1000000000000000055."""
    return Decimal(str(value))


def run_guard(inputs: GuardInputs) -> tuple[GuardDecision, Decimal]:
    """Call the project's real guard; return its decision plus the derived budget.

    WHY quantize: ``Portfolio.balance_usd`` and friends are ``DecimalField(...,
    decimal_places=2)``, so quantizing mirrors what PostgreSQL would actually
    hold and keeps the guard's verbatim reason strings faithful to production.

    WHY ``exposure``: rules 6 and 7 (total exposure, single-instrument
    concentration) are skipped when the guard is handed no exposure. Supplying it
    is what makes this page exercise the whole guard rather than its per-trade
    half: the keyword is optional in the signature for backwards compatibility,
    not because those limits were meant to be skipped.
    """
    portfolio = Portfolio(
        balance_usd=inputs.cash.quantize(CENT),
        max_trade_allocation_pct=inputs.allocation_pct.quantize(PCT_STEP),
        daily_loss_limit_usd=inputs.loss_limit.quantize(CENT),
        max_portfolio_exposure_pct=inputs.exposure_pct.quantize(PCT_STEP),
        max_ticker_concentration_pct=inputs.concentration_pct.quantize(PCT_STEP),
    )
    position = PositionState(ticker=inputs.ticker, amount=inputs.position_amount)
    exposure = Exposure(
        positions_value_usd=inputs.positions_value_usd,
        ticker_value_usd=inputs.ticker_value_usd,
        equity_usd=inputs.equity_usd,
    )
    decision = evaluate_proposal(
        portfolio=portfolio,
        proposal=Proposal(action=inputs.side, amount=float(inputs.amount)),
        price=inputs.price.quantize(CENT),
        position=position,
        realised_pnl_today=inputs.realised_pnl.quantize(CENT),
        exposure=exposure,
    )
    return decision, portfolio.max_trade_budget_usd


def _mirror_sell(inputs: GuardInputs) -> GuardDecision:
    """Re-run the real guard for the de-risking side of the same portfolio state.

    WHY: rule 5 freezes new risk but never blocks selling - that asymmetry is the
    entire point of a kill switch, and it is worth showing rather than claiming.
    The same holds for the two portfolio ceilings, which bound new risk only.
    """
    return run_guard(
        GuardInputs(
            cash=inputs.cash,
            allocation_pct=inputs.allocation_pct,
            loss_limit=inputs.loss_limit,
            realised_pnl=inputs.realised_pnl,
            price=inputs.price,
            side="SELL",
            amount=min(inputs.amount, inputs.position_amount) or inputs.position_amount,
            position_amount=inputs.position_amount,
            ticker=inputs.ticker,
            other_positions_value=inputs.other_positions_value,
            exposure_pct=inputs.exposure_pct,
            concentration_pct=inputs.concentration_pct,
        )
    )[0]


def _apply_preset(preset: Preset) -> None:
    """Button callback: callbacks run before the rerun, so widgets pick these values up."""
    for key, value in preset.values.items():
        st.session_state[key] = value


def _init_state() -> None:
    """Seed the widget defaults once, without clobbering a preset already applied."""
    for key, value in WIDGET_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _display_path(path: Path) -> str:
    """Repo-relative path when possible, so the caption is readable on Cloud too."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🛡️ AlphaAgent - try the execution guard")
st.markdown(
    "**AlphaAgent** is an autonomous AI investment platform: three LLM agents "
    "(a bull analyst, a bear short-seller and a CIO who adjudicates) debate a trade, "
    "and every resulting proposal must pass a deterministic risk guard before it can "
    "reach the transaction ledger. The guard enforces a per-trade allocation ceiling, two "
    "portfolio-level ceilings - total invested capital and single-instrument concentration, "
    "both measured against **equity** - and a daily-loss kill switch, and it is the only "
    "path from an AI proposal to real money."
)
st.info(
    "**Offline demo - no API keys, no database, no network calls.** The verdicts below are "
    "produced by the project's real guard, `services/execution.py::evaluate_proposal`: the same "
    "function the Celery worker, the REST API and the Telegram bot call. Nothing is "
    "reimplemented and no LLM is involved - the model's proposal is simply replaced by your "
    "sliders. Django is bootstrapped with `DATABASES={}` and the guard runs on unsaved model "
    "instances, so no database is ever opened."
)

# ---------------------------------------------------------------------------
# Guard playground
# ---------------------------------------------------------------------------
_init_state()

st.header("1. Guard playground")
st.caption(
    "Pick a preset to see a known guard path, then move the sliders. "
    "The right-hand panel is the guard's untouched return value."
)

for start in range(0, len(PRESETS), PRESETS_PER_ROW):
    preset_row = PRESETS[start : start + PRESETS_PER_ROW]
    for column, preset in zip(st.columns(len(preset_row)), preset_row, strict=True):
        with column:
            st.button(
                preset.label,
                key=f"preset::{preset.label}",
                on_click=_apply_preset,
                args=(preset,),
                help=preset.expectation,
                width="stretch",
            )

with st.expander("What each preset demonstrates"):
    for preset in PRESETS:
        st.markdown(f"**{preset.label}** - {preset.expectation}")

controls, verdict_panel = st.columns([1, 1], gap="large")

with controls:
    st.subheader("Portfolio risk configuration")
    st.slider(
        "Cash balance (USD)",
        min_value=0.0,
        max_value=100_000.0,
        step=500.0,
        key="cash",
        help="Portfolio.balance_usd - uninvested cash available to new positions.",
    )
    st.slider(
        "max_trade_allocation_pct (% of cash per trade)",
        min_value=0.0,
        max_value=25.0,
        step=0.25,
        key="allocation",
        help="Portfolio.max_trade_allocation_pct - the per-trade ceiling the guard enforces.",
    )
    st.slider(
        "daily_loss_limit_usd (kill switch)",
        min_value=0.0,
        max_value=5_000.0,
        step=50.0,
        key="loss_limit",
        help="Portfolio.daily_loss_limit_usd - once breached, new BUY risk is frozen.",
    )
    st.slider(
        "Realised P&L today (USD)",
        min_value=-5_000.0,
        max_value=2_000.0,
        step=25.0,
        key="realised",
        help="Passed to the guard as realised_pnl_today (negative = losses).",
    )
    st.slider(
        "max_portfolio_exposure_pct (% of equity invested)",
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        key="exposure_pct",
        help=(
            "Portfolio.max_portfolio_exposure_pct - caps TOTAL invested capital as a percentage "
            "of equity (cash + positions at market), not of cash. 0 disables the limit: the "
            "guard only applies a ceiling greater than zero."
        ),
    )
    st.slider(
        "max_ticker_concentration_pct (% of equity in one name)",
        min_value=0.0,
        max_value=100.0,
        step=1.0,
        key="concentration_pct",
        help=(
            "Portfolio.max_ticker_concentration_pct - caps ONE instrument, again as a percentage "
            "of equity. A portfolio 90% invested in a single stock passes the exposure limit "
            "and is not diversified at all, which is why this is a separate ceiling."
        ),
    )

    st.subheader("Existing positions (market values)")
    st.number_input(
        "Units already held (drives SELL limits)",
        min_value=0.0,
        max_value=1_000_000.0,
        step=1.0,
        key="position",
        help=(
            "services.ledger.PositionState reconstructed from the ledger. It also values this "
            "instrument for the concentration ceiling (units held x price)."
        ),
    )
    st.slider(
        "Other holdings, at market (USD, excluding this ticker)",
        min_value=0.0,
        max_value=200_000.0,
        step=50.0,
        key="other_positions",
        help=(
            "Everything the portfolio holds apart from this ticker. With this ticker's holding "
            "it forms Exposure.positions_value_usd."
        ),
    )
    st.caption(
        "`Exposure` is built from the widgets: **ticker value** = units held x price (the same "
        "price the guard prices the trade with), **positions** = that plus other holdings, "
        "**equity** = cash + positions. The guard never fetches these numbers - they are passed "
        "in, which is what keeps `evaluate_proposal` a pure function and this page free of "
        "market data."
    )

    st.subheader("The AI's proposal")
    st.text_input("Ticker", key="ticker", max_chars=10)
    st.radio("Side", options=["BUY", "SELL"], horizontal=True, key="side")
    st.number_input(
        "Proposed trade amount (units)",
        min_value=0.0,
        max_value=1_000_000.0,
        step=1.0,
        key="amount",
    )
    st.number_input(
        "Asset price (USD)",
        min_value=0.01,
        max_value=1_000_000.0,
        step=1.0,
        key="price",
        help="In production this comes from yfinance; the guard rejects any trade without a live price.",
    )

inputs = GuardInputs(
    cash=_dec(st.session_state["cash"]),
    allocation_pct=_dec(st.session_state["allocation"]),
    loss_limit=_dec(st.session_state["loss_limit"]),
    realised_pnl=_dec(st.session_state["realised"]),
    price=_dec(st.session_state["price"]),
    side=st.session_state["side"],
    amount=_dec(st.session_state["amount"]),
    position_amount=_dec(st.session_state["position"]),
    ticker=(st.session_state["ticker"] or "TICKER").strip().upper(),
    other_positions_value=_dec(st.session_state["other_positions"]),
    exposure_pct=_dec(st.session_state["exposure_pct"]),
    concentration_pct=_dec(st.session_state["concentration_pct"]),
)

decision, budget = run_guard(inputs)

with verdict_panel:
    st.subheader("The guard's verdict")
    if decision.approved:
        st.success(f"ALLOWED - {decision.reason}")
    else:
        st.error(f"REJECTED - {decision.reason}")

    if decision.clamped:
        st.warning("The guard clamped this order: " + " ".join(decision.notes))

    metric_cols = st.columns(3)
    metric_cols[0].metric("Per-trade budget", f"${budget:,.2f}")
    metric_cols[1].metric("Requested notional", f"${(inputs.amount * inputs.price):,.2f}")
    metric_cols[2].metric("Approved notional", f"${decision.notional:,.2f}")

    exposure_cols = st.columns(3)
    exposure_cols[0].metric(
        "Equity (cash + positions)",
        f"${inputs.equity_usd:,.2f}",
        help="Exposure.equity_usd - the denominator for both portfolio ceilings.",
    )
    # A ceiling of 0 means "disabled" in the guard, so the label must not read as
    # "nothing may be held".
    exposure_cols[1].metric(
        f"Positions (ceiling {inputs.exposure_pct:,.2f}% of equity)"
        if inputs.exposure_pct > 0
        else "Positions (ceiling disabled)",
        f"${inputs.positions_value_usd:,.2f}",
        help="Exposure.positions_value_usd - every holding at market, this ticker included.",
    )
    exposure_cols[2].metric(
        f"{inputs.ticker} (ceiling {inputs.concentration_pct:,.2f}% of equity)"
        if inputs.concentration_pct > 0
        else f"{inputs.ticker} (ceiling disabled)",
        f"${inputs.ticker_value_usd:,.2f}",
        help=(
            "Exposure.ticker_value_usd - units held x the price the guard trades at. "
            "The guard's note says when this rather than the exposure ceiling bound the trade."
        ),
    )
    st.caption(
        "Both portfolio ceilings are percentages of **equity**, not of cash and not of the "
        "per-trade budget — a percentage of cash would let a portfolio with a large position "
        "and little cash look empty."
    )

    st.markdown("**Raw `GuardDecision` returned by `evaluate_proposal`**")
    st.json(
        {
            "approved": decision.approved,
            "reason": decision.reason,
            "action": decision.action,
            "amount": str(decision.amount),
            "price": None if decision.price is None else str(decision.price),
            "notional": str(decision.notional),
            "clamped": decision.clamped,
            "notes": decision.notes,
        }
    )

    if inputs.side == "BUY" and not decision.approved:
        mirror = _mirror_sell(inputs)
        # Only the kill switch has a side asymmetry; other rules bind both sides,
        # so the note must not claim de-risking is available when it is not.
        asymmetry = (
            " - de-risking stays available while the kill switch is active"
            if "daily loss limit" in decision.reason
            else ""
        )
        st.caption(
            "Same portfolio state, opposite side - the guard's own answer for a SELL: "
            f"**{'ALLOWED' if mirror.approved else 'REJECTED'}** - {mirror.reason}{asymmetry}"
        )

with st.expander(
    f"Proof: the source of the function that produced this verdict ({_display_path(GUARD_SOURCE)})"
):
    st.caption(
        "Read from disk at runtime with `inspect.getsource`, so this is exactly the code "
        f"that just ran. Module: `{evaluate_proposal.__module__}`, "
        f"imported from `{_display_path(Path(inspect.getsourcefile(evaluate_proposal) or GUARD_SOURCE))}`."
    )
    st.code(inspect.getsource(evaluate_proposal), language="python")

st.divider()

# ---------------------------------------------------------------------------
# The debate that feeds the guard
# ---------------------------------------------------------------------------
st.header("2. What the guard is guarding: a three-agent adversarial debate")
st.markdown(
    "Proposals do not come from a single model. `ai_agent.py` runs three CrewAI agents in a "
    "**sequential** process, structured as a debate so that no single narrative reaches the "
    "decision unchallenged:"
)

bull, bear, cio = st.columns(3)
with bull:
    st.markdown("#### 🐂 Bullish Research Analyst")
    st.markdown(
        "Builds the strongest evidence-based **bull case** from the latest headlines and price "
        "action.\n\n- `NewsSentimentTool`\n- `PriceHistoryTool`"
    )
with bear:
    st.markdown("#### 🐻 Risk Assessor (Short Seller)")
    st.markdown(
        "Builds the **bear case** from independent evidence: leverage, liquidity, cash burn, "
        "valuation and technical breakdown.\n\n- `FinancialHealthTool`\n- `PriceHistoryTool`"
        "\n- `NewsSentimentTool`"
    )
with cio:
    st.markdown("#### ⚖️ Chief Investment Officer")
    st.markdown(
        "**Adjudicates** between the two arguments and decides BUY / SELL / HOLD given the live "
        "price and the investor's risk profile.\n\n- `MarketPriceTool` (yfinance)"
    )

st.markdown(
    """
The pipeline is deliberately one-way, and the guard is the only gate:

```
news + prices ──▶ 🐂 Bull case ──┐
                                 ├──▶ ⚖️ CIO: {action, amount, sentiment, reasoning}
fundamentals ──▶ 🐻 Bear case ───┘             │
                                               ▼
                          pydantic TradeProposal  (contract validation #1)
                                               │
                          CrewAI task guardrail   (contract validation #2)
                                               │
                          evaluate_proposal()     ◀── the guard you just drove
                                               │
                          apply_trade() ──▶ immutable Transaction ledger
```

* **No agent tool touches the database** - every tool is read-only and returns JSON.
* **HOLD never reaches the database**, and neither does a rejected proposal.
* **The guard is not the model's job**: a hallucinated size, or an overshoot past the
  per-trade ceiling, the portfolio exposure ceiling or the concentration ceiling, is
  clamped or refused by deterministic arithmetic, not by another LLM.
* Without an LLM credential the platform falls back to `HeuristicDecisionEngine`, a
  deterministic offline engine - which is why this demo needs no API key.
"""
)

with st.expander("Which guard rules this page exercises"):
    st.markdown(
        """
Every verdict above comes from `evaluate_proposal()`. Its docstring lists seven rules;
these widgets can reach five of them:

* **Rule 3 - per-trade allocation ceiling** (`max_trade_allocation_pct`, a percentage of
  **cash**): presets 1-3.
* **Rule 4 - SELL cannot exceed the held position**: preset 5.
* **Rule 5 - daily-loss kill switch** (BUY only): preset 4, and the mirrored SELL shows
  that de-risking stays open while it is active.
* **Rule 6 - portfolio exposure ceiling** (`max_portfolio_exposure_pct`, a percentage of
  **equity**): presets 6 and 8.
* **Rule 7 - single-instrument concentration ceiling** (`max_ticker_concentration_pct`,
  also a percentage of **equity**): presets 7, 8 and 9.

Rules 6 and 7 are the easiest to lose: **the guard skips both when it is given no
`exposure`**, or when equity is zero (`Exposure.usable` is false), and an earlier version of
this page passed none - so the demo exercised only the per-trade half of the guard while
looking complete. It now always builds an `Exposure`, from the widgets:

```
ticker_value_usd    = units held x price            this instrument, at market
positions_value_usd = that + other holdings         every holding, at market
equity_usd          = cash + positions_value_usd    the denominator for both ceilings
```

Both ceilings are percentages of that **equity** - not of cash, and not of the per-trade
budget, which is a percentage of cash. A BUY that does not fit is **sized down** to what
does, and the clamp note names the ceiling that bound it; only a BUY with no headroom at
all is refused, and that refusal is the guard's shared `$0` "cannot be sized down" message
rather than a rule-specific one.

Not reachable from these widgets: **rule 1** (the side selector offers BUY and SELL only, so
a HOLD never becomes a proposal on this page), **rule 2** (the price widget's minimum is
$0.01, so "no reliable market price" cannot be produced) and the BUY-versus-cash half of
**rule 4** (the per-trade ceiling is at most 25% of cash, so a clamped BUY always fits the
balance).
"""
    )

with st.expander("Files this demo actually exercises"):
    st.markdown(
        f"""
| Path | Role in this demo |
| --- | --- |
| `{_display_path(GUARD_SOURCE)}` | **Executed.** `evaluate_proposal()` and `Exposure` - the real, unmodified guard. |
| `services/ledger.py` | **Executed.** `PositionState`, the real position dataclass. |
| `core/models.py` | **Executed.** `Portfolio` - instantiated unsaved; `max_trade_budget_usd` is its real property, and both ceiling fields are set from the sliders. |
| `services/portfolio_metrics.py` | **Not executed.** `build_exposure()` is how the production callers derive the same three market values; here they come from the widgets. |
| `services/attribution.py` | **Not executed, not imported** - it reads `DecisionOutcome` rows and price history. Explained in section 3. |
| `services/thesis.py` | **Not executed, not imported** - it reads and writes `PositionThesis` through the ORM. Explained in section 4. |
| `ai_agent.py` | Not executed (needs CrewAI + an LLM); the proposal payload is modelled by a two-field stand-in. |
| `tasks.py` | Not executed (Celery + PostgreSQL); it is the caller that persists an approved decision. |
"""
    )

st.divider()

# ---------------------------------------------------------------------------
# Decision attribution: explained, deliberately not executed
# ---------------------------------------------------------------------------
st.header("3. Judging the decisions afterwards: outcome attribution")
st.info(
    "**Not executed on this page.** Scoring a decision needs the price history that followed "
    "it, so `services/attribution.py` reads `AgentDecisionLog` / `DecisionOutcome` rows and "
    "calls yfinance. This demo has no database and makes no network calls, so the module is "
    "**not imported** - what follows describes the real implementation rather than simulating "
    "it. The guard in section 1 is the only part of the platform this page actually executes."
)

horizons, scoring, benchmark = st.columns(3)
with horizons:
    st.markdown("#### Forward returns at 1, 7 and 30 days")
    st.markdown(
        "* `DecisionOutcome.HORIZONS = (1, 7, 30)`: each scored decision gets one row per "
        "horizon.\n"
        "* Scoring is idempotent - a (decision, horizon) pair that is already scored is "
        "skipped, so running the sweep more often than necessary costs nothing but time.\n"
        "* The starting price is the transaction's own price whenever there is one, not that "
        "day's close: measuring from the close would score the trade at a price it never got."
    )
with scoring:
    st.markdown("#### What the action was worth")
    st.markdown(
        "* A decision is scored by the value of the **action**, not by the direction of the "
        "price: a BUY is right when the price rises, a **SELL is right when the price falls** "
        '(`signed_return`). Scoring everything against "did the price go up" would mark every '
        "correct exit as a failure.\n"
        "* A **HOLD makes no directional claim**: its `directional_hit` is `None`, not `False`, "
        "and HOLDs are **excluded from the hit rate** instead of counted as misses - counting "
        "them would drag the measured hit rate toward zero in proportion to how cautious the "
        "system was. They still carry a `decision_return_pct` of zero, so they stay visible in "
        "the average."
    )
with benchmark:
    st.markdown("#### SPY as the benchmark")
    st.markdown(
        "* The benchmark is **SPY**, recorded per decision; alpha is the decision return minus "
        "the benchmark return over the same window.\n"
        "* A missing benchmark is **null, never zero** - an unknown comparison is not a loss.\n"
        "* Scorecards are computed on read (`build_scorecard`) rather than stored, so a "
        "re-evaluation shows up immediately, and an empty scorecard reports a hit rate of "
        "`None` rather than `0%`."
    )

st.caption(
    "Together with the audit trail, this closes the loop the platform was missing: the log "
    "answers *why* a decision was made, attribution answers *whether it worked*. This one needs "
    "a database and price history, which is why it is described here and not driven."
)

st.divider()

# ---------------------------------------------------------------------------
# Position theses: explained, deliberately not executed
# ---------------------------------------------------------------------------
st.header("4. Remembering why we hold: position theses")
st.info(
    "**Not executed on this page.** A thesis is a row in the `PositionThesis` table and "
    "`services/thesis.py` reads and writes it through the Django ORM, which needs a database. "
    "The module is **not imported** here - again a description of the real implementation, not "
    "a simulation of it."
)

st.markdown(
    "The crew runs with `memory=False` (ADR-0007), so every sweep starts from nothing: the "
    "agents could sell at 10:00 and rebuy at 10:15 without noticing, not because they changed "
    "their mind but because they had no record of having one. A thesis is the narrow fix - "
    "**one durable paragraph per open position**, written by the agent that opened it."
)

st.markdown(
    """
* **Written on a BUY, and only on a BUY.** The CIO fills in `thesis` (why this is worth
  holding) and `invalidation` (the observable condition that would prove it wrong); a thesis
  that cannot be wrong is not a thesis. Both fields are optional on the proposal model so a
  model that omits them still validates - but a blank thesis is never recorded, because a
  placeholder would be fed back to the agents as their own prior belief.
* **Fed to the agents on every run.** The live thesis arrives in the agents' brief as a
  `PRIOR THESIS (recorded when this position was opened)` block, with the stated invalidation
  condition. When there is none, the brief says so explicitly rather than leaving a blank
  section a model could fill in with an invented one.
* **An exit has to argue with the reason for entry.** When a prior thesis exists and the CIO
  proposes to SELL, the prompt requires it to state what has stopped being true - so a
  reversal is a change of mind by argument rather than by accident.
* **Superseded, never edited.** A new thesis on the same position closes the previous row
  (`close_reason="Superseded by a new thesis on the same position."`) instead of overwriting
  it, so the table reads as a history of what was believed and when.
* **Closed with the position.** A SELL that empties the position closes the live thesis;
  leaving it open would feed a stale belief into the next run as though it were current.
  `sync_after_trade()` is the single entry point that keeps the two in step.
* **Visible outside the crew**: the dashboard renders the open theses next to the positions,
  and `GET /api/portfolio/thesis/` returns them (`?include=closed` adds the superseded ones).
"""
)

st.caption(
    "Note what this is *not*: not a conversational memory, which would grow without bound and "
    "make runs irreproducible (ADR-0007), just one paragraph per open position that keeps a "
    "decision and the reason for it together."
)

st.divider()
st.caption(
    f"AlphaAgent - autonomous multi-agent investment platform · "
    f"[github.com/SergeyGer/AlphaAgent]({GITHUB_URL}) · "
    "this page runs the project's real risk guard offline - per-trade, portfolio exposure, "
    "concentration and daily-loss limits included - with no database and no API keys."
)
