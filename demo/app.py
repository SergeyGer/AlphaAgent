"""AlphaAgent offline demo - the project's *real* execution guard, no API keys, no database.

WHY this file is so thin: the safety-critical heart of AlphaAgent
(``services.execution.evaluate_proposal``) is a pure function that performs no
I/O, but it does import Django models. So instead of reimplementing the
guardrails for a demo (which would let the demo drift away from the real
behaviour), the app configures the smallest possible Django settings module at
import time and calls the genuine function - on **unsaved** model instances,
which never touch a database. Only the *inputs* are wired to widgets here.

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
from services.execution import GuardDecision, evaluate_proposal  # noqa: E402
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


PRESETS: tuple[Preset, ...] = (
    Preset(
        label="1 - Normal trade (allowed)",
        expectation="3 units @ $100 = $300 against a $500 per-trade budget: approved untouched.",
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
        },
    ),
    Preset(
        label="2 - Breaches the allocation ceiling (clamped)",
        expectation="$5,000 order against a $500 ceiling: the guard sizes it down to $500.",
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 50.0,
            "position": 10.0,
        },
    ),
    Preset(
        label="3 - Ceiling leaves $0 budget (rejected)",
        expectation="max_trade_allocation_pct = 0%: no trade can fit, so every BUY is refused.",
        values={
            "cash": 10_000.0,
            "allocation": 0.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
        },
    ),
    Preset(
        label="4 - Daily loss limit already hit (rejected)",
        expectation="-$600 realised against a $500 kill switch: new risk is frozen.",
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": -600.0,
            "price": 100.0,
            "side": "BUY",
            "amount": 3.0,
            "position": 10.0,
        },
    ),
    Preset(
        label="5 - Oversized SELL (clamped to position)",
        expectation="Selling 50 units while holding 10: clamped to the position, never short.",
        values={
            "cash": 10_000.0,
            "allocation": 5.0,
            "loss_limit": 500.0,
            "realised": 0.0,
            "price": 100.0,
            "side": "SELL",
            "amount": 50.0,
            "position": 10.0,
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


def _dec(value: float | str) -> Decimal:
    """Convert a widget float via ``str`` so 0.1 does not arrive as 0.1000000000000000055."""
    return Decimal(str(value))


def run_guard(inputs: GuardInputs) -> tuple[GuardDecision, Decimal]:
    """Call the project's real guard; return its decision plus the derived budget.

    WHY quantize: ``Portfolio.balance_usd`` and friends are ``DecimalField(...,
    decimal_places=2)``, so quantizing mirrors what PostgreSQL would actually
    hold and keeps the guard's verbatim reason strings faithful to production.
    """
    portfolio = Portfolio(
        balance_usd=inputs.cash.quantize(CENT),
        max_trade_allocation_pct=inputs.allocation_pct.quantize(PCT_STEP),
        daily_loss_limit_usd=inputs.loss_limit.quantize(CENT),
    )
    position = PositionState(ticker=inputs.ticker, amount=inputs.position_amount)
    decision = evaluate_proposal(
        portfolio=portfolio,
        proposal=Proposal(action=inputs.side, amount=float(inputs.amount)),
        price=inputs.price.quantize(CENT),
        position=position,
        realised_pnl_today=inputs.realised_pnl.quantize(CENT),
    )
    return decision, portfolio.max_trade_budget_usd


def _mirror_sell(inputs: GuardInputs) -> GuardDecision:
    """Re-run the real guard for the de-risking side of the same portfolio state.

    WHY: rule 5 freezes new risk but never blocks selling - that asymmetry is the
    entire point of a kill switch, and it is worth showing rather than claiming.
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
    "reach the transaction ledger. The guard enforces a per-trade allocation ceiling "
    "and a daily-loss kill switch, and it is the only path from an AI proposal to real money."
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

preset_cols = st.columns(len(PRESETS))
for column, preset in zip(preset_cols, PRESETS, strict=True):
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
    st.number_input(
        "Units already held (drives SELL limits)",
        min_value=0.0,
        max_value=1_000_000.0,
        step=1.0,
        key="position",
        help="services.ledger.PositionState reconstructed from the ledger.",
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
* **The guard is not the model's job**: a hallucinated size or an overshoot past the
  per-trade ceiling is clamped or refused by deterministic arithmetic, not by another LLM.
* Without an LLM credential the platform falls back to `HeuristicDecisionEngine`, a
  deterministic offline engine - which is why this demo needs no API key.
"""
)

with st.expander("Files this demo actually exercises"):
    st.markdown(
        f"""
| Path | Role in this demo |
| --- | --- |
| `{_display_path(GUARD_SOURCE)}` | **Executed.** `evaluate_proposal()` - the real, unmodified guard. |
| `services/ledger.py` | **Executed.** `PositionState`, the real position dataclass. |
| `core/models.py` | **Executed.** `Portfolio` - instantiated unsaved; `max_trade_budget_usd` is its real property. |
| `ai_agent.py` | Not executed (needs CrewAI + an LLM); the proposal payload is modelled by a two-field stand-in. |
| `tasks.py` | Not executed (Celery + PostgreSQL); it is the caller that persists an approved decision. |
"""
    )

st.divider()
st.caption(
    f"AlphaAgent - autonomous multi-agent investment platform · "
    f"[github.com/SergeyGer/AlphaAgent]({GITHUB_URL}) · "
    "this page runs the project's real risk guard offline, with no database and no API keys."
)
