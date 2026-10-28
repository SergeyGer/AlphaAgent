"""AlphaAgent offline demo - the real execution guard, wearing the product's dashboard.

WHY this file exists: the safety-critical heart of AlphaAgent
(``services.execution.evaluate_proposal``) is a pure function that performs no
I/O, but it does import Django models. Rather than reimplement the guardrails for
a demo (which would let the demo drift away from the real behaviour), the app
configures the smallest possible Django settings module at import time and calls
the genuine function - on **unsaved** model instances, which never touch a
database. Only the *inputs* are wired to widgets here.

The page is shaped like the React terminal at ``/api``: a dark top bar, seven
metric cards, the two portfolio-ceiling meters, then tabs. The chrome is plain
HTML/CSS emitted through ``st.markdown`` so it cannot drift with Streamlit's
internal element classes; native widgets are kept for the inputs that must be
interactive.

Every input the guard takes is supplied, including the market values it refuses
to fetch itself (``services.execution.Exposure``). That matters: given no
exposure the guard skips its two portfolio-level ceilings, so a page that omitted
it would be driving only half the guard.

Run locally:  streamlit run demo/app.py
"""

from __future__ import annotations

import html
import inspect
import json
import os
import sys
from collections.abc import Sequence
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

# (widget key, minimum, maximum) for every st.slider on the page. Used only to
# draw each slider's visited portion in the dashboard's emerald; the widget
# itself still owns the value.
SLIDER_RANGES: tuple[tuple[str, float, float], ...] = (
    ("cash", 0.0, 100_000.0),
    ("allocation", 0.0, 25.0),
    ("loss_limit", 0.0, 5_000.0),
    ("realised", -5_000.0, 2_000.0),
    ("exposure_pct", 0.0, 100.0),
    ("concentration_pct", 0.0, 100.0),
    ("other_positions", 0.0, 200_000.0),
)

st.set_page_config(
    page_title="AlphaAgent · Autonomous Investment Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
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
    short: str
    expectation: str
    values: dict[str, Any]


# Every preset states the same four portfolio facts (cash, what else is held,
# what this ticker is worth via units held x price) plus the two ceilings, so each
# one shows a single interesting guard path rather than a combination.
PRESETS: tuple[Preset, ...] = (
    Preset(
        label="1 - Normal trade (allowed)",
        short="01 · Normal trade",
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
        short="02 · Allocation clamp",
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
        short="03 · No budget",
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
        short="04 · Loss limit hit",
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
        short="05 · Oversized sell",
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
        short="06 · Exposure clamp",
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
        short="07 · Concentration clamp",
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
        short="08 · Both ceilings",
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
        short="09 · Concentration full",
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
#
# ``cost_basis`` is the one value here that is NOT a guard input: it exists only
# so the UNREALISED P&L card can be computed from a real cost basis instead of a
# invented number. It is labelled "display only" where it is rendered.
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
    "cost_basis": 100.0,
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


def _dec(value: float | int | str) -> Decimal:
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


def _inputs_from_state() -> GuardInputs:
    """Read the committed widget state into the units the guard expects.

    WHY this can run before the widgets are declared: ``_init_state`` has already
    seeded every key, and Streamlit commits a widget's new value before rerunning.
    Computing the guard once per rerun is also what lets the metric row and the
    verdict panel describe the exact same decision.
    """
    state = st.session_state
    return GuardInputs(
        cash=_dec(state["cash"]),
        allocation_pct=_dec(state["allocation"]),
        loss_limit=_dec(state["loss_limit"]),
        realised_pnl=_dec(state["realised"]),
        price=_dec(state["price"]),
        side=state["side"],
        amount=_dec(state["amount"]),
        position_amount=_dec(state["position"]),
        ticker=(state["ticker"] or "TICKER").strip().upper(),
        other_positions_value=_dec(state["other_positions"]),
        exposure_pct=_dec(state["exposure_pct"]),
        concentration_pct=_dec(state["concentration_pct"]),
    )


def _slider_fill_css() -> str:
    """Colour each slider's visited track emerald.

    WHY generated: Streamlit draws the track with an inline ``linear-gradient``
    whose stop is the current value, using a theme colour we cannot change at
    runtime. Re-emitting the gradient with the same stop lets the accent match
    the dashboard without touching the widget's behaviour or its value.
    """
    rules = []
    for key, minimum, maximum in SLIDER_RANGES:
        value = float(st.session_state[key])
        span = maximum - minimum
        pct = 0.0 if span <= 0 else max(0.0, min(100.0, (value - minimum) / span * 100))
        rules.append(
            f'.st-key-{key} [data-testid="stSlider"] [role="group"] > div > div:first-child {{'
            " background: linear-gradient(to right, var(--aa-emerald) 0%, var(--aa-emerald)"
            f" {pct:.2f}%, #1E293B {pct:.2f}%, #1E293B 100%) !important; }}"
        )
    return "<style>" + "".join(rules) + "</style>"


def _display_path(path: Path) -> str:
    """Repo-relative path when possible, so the caption is readable on Cloud too."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Formatting helpers - the same shapes the React dashboard renders
# ---------------------------------------------------------------------------
def _e(value: object) -> str:
    """Escape anything that came from a widget or from the guard."""
    return html.escape(str(value))


def _money(value: Decimal | float | int) -> str:
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def _units(value: Decimal) -> str:
    """Trim a Decimal quantity without scientific notation (10.00000000 -> 10)."""
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _signed_money(value: Decimal | float | int) -> str:
    if value > 0:
        return f"+${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return "$0.00"


def _pct(value: Decimal | float | int, *, signed: bool = False) -> str:
    if signed and value > 0:
        return f"+{value:,.2f}%"
    return f"{value:,.2f}%"


def _ratio_pct(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole <= 0:
        return None
    return (part / whole * 100).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Dashboard numbers, all derived from the guard inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DashboardNumbers:
    """Everything the chrome displays, computed from the same inputs as the verdict."""

    equity: Decimal
    positions_value: Decimal
    ticker_value: Decimal
    position_cost: Decimal
    unrealised: Decimal
    unrealised_pct: Decimal | None
    open_positions: int
    exposure_ceiling: Decimal | None
    exposure_used_pct: Decimal | None
    exposure_ratio: Decimal | None
    concentration_ceiling: Decimal | None
    concentration_used_pct: Decimal | None
    concentration_ratio: Decimal | None
    loss_used: Decimal
    loss_remaining: Decimal | None
    loss_ratio: Decimal | None
    equity_share_pct: Decimal | None


def _dashboard_numbers(inputs: GuardInputs, cost_basis: Decimal) -> DashboardNumbers:
    """Derive the cards and meters without inventing a value.

    ``cost_basis`` is the visitor's display-only input for the P&L card. Other
    holdings are entered at market, so they are treated as carrying no unrealised
    P&L; the card therefore measures this instrument against its own cost basis,
    exactly as the widget help says.
    """
    equity = inputs.equity_usd
    positions = inputs.positions_value_usd
    ticker_value = inputs.ticker_value_usd
    ticker_cost = (inputs.position_amount * cost_basis).quantize(CENT)
    position_cost = (ticker_cost + inputs.other_positions_value).quantize(CENT)
    unrealised = (positions - position_cost).quantize(CENT)
    unrealised_pct = _ratio_pct(unrealised, position_cost)
    open_positions = int(inputs.position_amount > 0) + int(inputs.other_positions_value > 0)

    exposure_ceiling: Decimal | None = None
    exposure_ratio: Decimal | None = None
    if inputs.exposure_pct > 0 and equity > 0:
        exposure_ceiling = (equity * inputs.exposure_pct / 100).quantize(CENT)
        if exposure_ceiling > 0:
            exposure_ratio = positions / exposure_ceiling

    concentration_ceiling: Decimal | None = None
    concentration_ratio: Decimal | None = None
    if inputs.concentration_pct > 0 and equity > 0:
        concentration_ceiling = (equity * inputs.concentration_pct / 100).quantize(CENT)
        if concentration_ceiling > 0:
            concentration_ratio = ticker_value / concentration_ceiling

    loss_used = (-inputs.realised_pnl).quantize(CENT) if inputs.realised_pnl < 0 else Decimal("0.00")
    loss_remaining: Decimal | None = None
    loss_ratio: Decimal | None = None
    if inputs.loss_limit > 0:
        loss_remaining = (inputs.loss_limit - loss_used).quantize(CENT)
        loss_ratio = loss_used / inputs.loss_limit

    return DashboardNumbers(
        equity=equity,
        positions_value=positions,
        ticker_value=ticker_value,
        position_cost=position_cost,
        unrealised=unrealised,
        unrealised_pct=unrealised_pct,
        open_positions=open_positions,
        exposure_ceiling=exposure_ceiling,
        exposure_used_pct=_ratio_pct(positions, equity),
        exposure_ratio=exposure_ratio,
        concentration_ceiling=concentration_ceiling,
        concentration_used_pct=_ratio_pct(ticker_value, equity),
        concentration_ratio=concentration_ratio,
        loss_used=loss_used,
        loss_remaining=loss_remaining,
        loss_ratio=loss_ratio,
        equity_share_pct=_ratio_pct(inputs.cash, equity),
    )


# ---------------------------------------------------------------------------
# CSS - the dashboard's visual language, applied to Streamlit's shell
# ---------------------------------------------------------------------------
DASHBOARD_CSS = """
<style>
:root {
  --aa-bg: #0A0F1A;
  --aa-panel: #111827;
  --aa-panel-deep: #0B1220;
  --aa-border: #1F2937;
  --aa-text: #E2E8F0;
  --aa-muted: #94A3B8;
  --aa-label: #64748B;
  --aa-emerald: #34D399;
  --aa-rose: #FB7185;
  --aa-cyan: #22D3EE;
  --aa-amber: #FBBF24;
  --aa-mono: ui-monospace, "SF Mono", SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  --aa-sans: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}

/* --- Streamlit shell ---------------------------------------------------- */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: var(--aa-bg) !important;
  color: var(--aa-text);
}
.stApp { font-family: var(--aa-sans); }
[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
#MainMenu, footer { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1440px;
  padding: 1rem 1.4rem 3rem;
}
[data-testid="stVerticalBlock"] { gap: 0.55rem; }
[data-testid="stMarkdownContainer"] { color: var(--aa-muted); }
[data-testid="stMarkdownContainer"] a { color: var(--aa-emerald); text-decoration: none; }
[data-testid="stMarkdownContainer"] a:hover { text-decoration: underline; }
[data-testid="stCaptionContainer"] p { color: var(--aa-label); font-size: 0.68rem; }
* { scrollbar-width: thin; scrollbar-color: #334155 transparent; }
*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-thumb { background: #1E293B; border-radius: 9999px; }

/* --- Top bar ------------------------------------------------------------ */
.aa-topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 0.9rem;
  flex-wrap: wrap; padding: 0.2rem 0.05rem 0.8rem;
  border-bottom: 1px solid rgba(31, 41, 55, 0.9); margin-bottom: 0.15rem;
}
.aa-brand { display: flex; align-items: center; gap: 0.7rem; min-width: 0; }
.aa-logo {
  width: 2.15rem; height: 2.15rem; border-radius: 0.6rem; display: grid; place-items: center;
  background: linear-gradient(135deg, #34D399 0%, #22D3EE 60%, #0EA5E9 100%);
  color: #04120C; font-family: var(--aa-mono); font-size: 1.15rem; font-weight: 800;
  box-shadow: 0 0 26px rgba(52, 211, 153, 0.18);
}
.aa-brand-name {
  font-size: 0.98rem; font-weight: 650; letter-spacing: -0.01em; color: #F1F5F9;
  line-height: 1.15;
}
.aa-brand-sub {
  font-family: var(--aa-mono); font-size: 0.56rem; letter-spacing: 0.2em;
  text-transform: uppercase; color: var(--aa-label); margin-top: 0.12rem;
}
.aa-pills { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
.aa-pill {
  display: inline-flex; align-items: center; gap: 0.35rem; border: 1px solid var(--aa-border);
  background: rgba(17, 24, 39, 0.7); border-radius: 0.4rem; padding: 0.28rem 0.5rem;
  font-family: var(--aa-mono); font-size: 0.58rem; letter-spacing: 0.12em;
  text-transform: uppercase; color: #94A3B8; white-space: nowrap;
}
.aa-pill--live {
  color: #6EE7B7; border-color: rgba(52, 211, 153, 0.35); background: rgba(52, 211, 153, 0.08);
}
.aa-pill--cyan {
  color: #67E8F9; border-color: rgba(34, 211, 238, 0.3); background: rgba(34, 211, 238, 0.06);
}
.aa-pill--ghost { color: #64748B; }
.aa-live-dot {
  width: 0.42rem; height: 0.42rem; border-radius: 50%; background: var(--aa-emerald);
  box-shadow: 0 0 9px 1px rgba(52, 211, 153, 0.7); animation: aa-pulse 2.4s ease-in-out infinite;
}
@keyframes aa-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* --- Notices ------------------------------------------------------------ */
.aa-alert {
  display: flex; align-items: flex-start; gap: 0.55rem; padding: 0.5rem 0.75rem;
  border: 1px solid rgba(34, 211, 238, 0.26); border-radius: 0.55rem;
  background: rgba(34, 211, 238, 0.05); margin-bottom: 0.1rem;
}
.aa-alert-mark {
  font-family: var(--aa-mono); font-size: 0.68rem; color: var(--aa-cyan);
  line-height: 1.55; flex-shrink: 0;
}
.aa-alert p { margin: 0; font-size: 0.73rem !important; line-height: 1.55; color: #BAE6FD; }
.aa-alert strong { color: #E0F2FE; font-weight: 650; }
.aa-alert code {
  font-family: var(--aa-mono); font-size: 0.68rem; color: #A5F3FC;
  background: rgba(34, 211, 238, 0.08); padding: 0.05rem 0.25rem; border-radius: 0.2rem;
}

/* --- Metric cards ------------------------------------------------------- */
.aa-metrics {
  display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 0.5rem;
}
.aa-metric {
  min-width: 0; display: flex; flex-direction: column; gap: 0.18rem;
  border: 1px solid var(--aa-border); border-radius: 0.65rem;
  background: linear-gradient(180deg, #131C2E, #0F172A);
  padding: 0.58rem 0.68rem 0.62rem;
}
.aa-label {
  margin: 0; font-family: var(--aa-mono); font-size: 0.6rem !important; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--aa-label); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
.aa-metric-value {
  margin: 0; font-family: var(--aa-mono); font-weight: 650; letter-spacing: -0.01em;
  font-size: clamp(1rem, 1.25vw, 1.32rem) !important; line-height: 1.35; color: var(--aa-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.aa-metric-hint {
  margin: 0; font-size: 0.63rem !important; line-height: 1.35; color: var(--aa-label);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.aa-tone--emerald { color: var(--aa-emerald) !important; }
.aa-tone--rose { color: var(--aa-rose) !important; }
.aa-tone--cyan { color: var(--aa-cyan) !important; }
.aa-tone--amber { color: var(--aa-amber) !important; }
.aa-tone--slate { color: var(--aa-label) !important; }

/* --- Progress bars ------------------------------------------------------ */
.aa-bar {
  width: 100%; height: 0.3rem; border-radius: 9999px; background: #1E293B;
  overflow: hidden; margin: 0.16rem 0 0.1rem;
}
.aa-bar-fill {
  display: block; height: 100%; border-radius: 9999px; background: var(--aa-emerald);
}
.aa-bar-fill--rose { background: var(--aa-rose); }
.aa-bar-fill--cyan { background: var(--aa-cyan); }
.aa-bar-fill--slate { background: #334155; }

/* --- Ceiling meters ----------------------------------------------------- */
.aa-meters {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.35rem 1.6rem;
  border: 1px solid var(--aa-border); border-radius: 0.6rem;
  background: #0F172A; padding: 0.5rem 0.8rem 0.62rem;
}
.aa-meter { min-width: 0; }
.aa-meter-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem;
}
.aa-meter-value {
  font-family: var(--aa-mono); font-size: 0.7rem; font-weight: 650; color: var(--aa-text);
  white-space: nowrap;
}
.aa-meter-hint { margin: 0.16rem 0 0; font-size: 0.65rem !important; line-height: 1.45; color: var(--aa-label); }
.aa-over { color: var(--aa-rose); }
.aa-meter-hint .aa-ok { color: #94A3B8; }

/* --- Meta strip --------------------------------------------------------- */
.aa-meta {
  display: flex; flex-wrap: wrap; gap: 0.25rem 1.15rem; align-items: center;
  border: 1px solid var(--aa-border); border-radius: 0.5rem;
  background: #0F172A; padding: 0.4rem 0.7rem;
  font-family: var(--aa-mono); font-size: 0.56rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: #475569;
}
.aa-meta span { display: inline-flex; gap: 0.35rem; }
.aa-meta dt { margin: 0; color: #475569; }
.aa-meta dd { margin: 0; color: #CBD5E1; }
.aa-meta dd.aa-meta-accent { color: #6EE7B7; }

/* --- Tabs --------------------------------------------------------------- */
[data-testid="stTabs"] [role="tablist"] {
  gap: 0.1rem; border-bottom: 1px solid var(--aa-border); background: transparent;
}
[data-testid="stTab"] {
  font-family: var(--aa-mono) !important; font-size: 0.62rem !important; font-weight: 650;
  letter-spacing: 0.13em; text-transform: uppercase; color: var(--aa-label) !important;
  padding: 0.55rem 0.72rem !important; height: auto; background: transparent !important;
  border-radius: 0.35rem 0.35rem 0 0;
}
[data-testid="stTab"]:hover { color: #CBD5E1 !important; background: rgba(17, 24, 39, 0.6) !important; }
[data-testid="stTab"] p {
  color: var(--aa-label) !important; font-size: 0.63rem !important;
  letter-spacing: 0.13em !important; text-transform: uppercase !important;
  margin: 0 !important; line-height: 1.4 !important;
}
[data-testid="stTab"][aria-selected="true"] { color: var(--aa-emerald) !important; }
[data-testid="stTab"][aria-selected="true"] p { color: var(--aa-emerald) !important; }
[data-testid="stTab"] .react-aria-SelectionIndicator {
  background-color: var(--aa-emerald) !important; height: 2px !important;
}
[data-testid="stTabPanel"] { padding-top: 0.35rem; }

/* --- Keyed containers become dashboard panels -------------------------- */
.st-key-aa-presets, .st-key-aa-controls, .st-key-aa-verdict {
  border: 1px solid var(--aa-border); border-radius: 0.7rem;
  background: linear-gradient(180deg, #131C2E, #0F172A);
  padding: 0.72rem 0.82rem 0.82rem;
}

/* --- Static panels and cards ------------------------------------------- */
.aa-panel {
  border: 1px solid var(--aa-border); border-radius: 0.65rem;
  background: linear-gradient(180deg, #131C2E, #0F172A);
  padding: 0.68rem 0.8rem 0.78rem;
}
.aa-panel-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 0.8rem;
  margin-bottom: 0.55rem;
}
.aa-panel-title {
  margin: 0; padding: 0; font-family: var(--aa-mono); font-size: 0.63rem !important;
  font-weight: 650; letter-spacing: 0.15em; text-transform: uppercase; color: #CBD5E1;
}
.aa-panel-caption { margin: 0.14rem 0 0; font-size: 0.68rem !important; line-height: 1.45; color: var(--aa-label); }
.aa-panel-body { font-size: 0.71rem; line-height: 1.6; color: var(--aa-muted); }
.aa-panel-body p { margin: 0 0 0.4rem; }
.aa-panel-body p:last-child { margin-bottom: 0; }
.aa-panel-body strong { color: var(--aa-text); font-weight: 600; }
.aa-panel-body code, .aa-card-body code, .aa-thesis code, .aa-score code {
  font-family: var(--aa-mono); font-size: 0.66rem; color: #A5F3FC;
  background: rgba(34, 211, 238, 0.07); padding: 0.05rem 0.24rem; border-radius: 0.2rem;
}
.aa-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.8rem; }
.aa-section-head .aa-panel-title { font-size: 0.64rem !important; }
.aa-group-label {
  margin: 0.3rem 0 0.05rem; font-family: var(--aa-mono); font-size: 0.57rem !important;
  letter-spacing: 0.15em; text-transform: uppercase; color: #64748B;
  border-top: 1px solid rgba(31, 41, 55, 0.72); padding-top: 0.55rem;
  line-height: 1.4;
}
.aa-group-label:first-of-type { border-top: 0; padding-top: 0; }
.aa-card-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.55rem; }
.aa-card {
  min-width: 0; border: 1px solid rgba(31, 41, 55, 0.9); border-radius: 0.55rem;
  background: #0F172A; padding: 0.58rem 0.65rem;
}
.aa-card--bull { border-left: 2px solid rgba(52, 211, 153, 0.55); }
.aa-card--bear { border-left: 2px solid rgba(251, 113, 133, 0.55); }
.aa-card--verdict { border-left: 2px solid rgba(100, 116, 139, 0.6); }
.aa-card-title {
  margin: 0 0 0.3rem; font-family: var(--aa-mono); font-size: 0.57rem !important; font-weight: 650;
  letter-spacing: 0.14em; text-transform: uppercase; color: #94A3B8;
}
.aa-card--bull .aa-card-title { color: #6EE7B7; }
.aa-card--bear .aa-card-title { color: #FDA4AF; }
.aa-card-body { font-size: 0.68rem; line-height: 1.55; color: var(--aa-muted); }
.aa-card-body p { margin: 0 0 0.3rem; }
.aa-card-body p:last-child { margin: 0; }
.aa-list { margin: 0.25rem 0 0; padding-left: 0.95rem; }
.aa-list li { margin: 0.12rem 0; font-size: 0.69rem !important; line-height: 1.55; color: var(--aa-muted); }
.aa-list li::marker { color: #475569; }
.aa-list strong { color: var(--aa-text); }

/* --- Badges, verdict and raw decision ---------------------------------- */
.aa-badge {
  display: inline-flex; align-items: center; gap: 0.3rem; border-radius: 0.3rem;
  border: 1px solid rgba(100, 116, 139, 0.4); background: rgba(100, 116, 139, 0.08);
  padding: 0.14rem 0.38rem; font-family: var(--aa-mono); font-size: 0.55rem;
  font-weight: 650; letter-spacing: 0.1em; text-transform: uppercase; color: #CBD5E1;
  white-space: nowrap;
}
.aa-badge--emerald { color: #6EE7B7; border-color: rgba(52, 211, 153, 0.35); background: rgba(52, 211, 153, 0.09); }
.aa-badge--rose { color: #FDA4AF; border-color: rgba(251, 113, 133, 0.4); background: rgba(251, 113, 133, 0.09); }
.aa-badge--cyan { color: #67E8F9; border-color: rgba(34, 211, 238, 0.32); background: rgba(34, 211, 238, 0.07); }
.aa-badge--amber { color: #FCD34D; border-color: rgba(251, 191, 36, 0.35); background: rgba(251, 191, 36, 0.08); }
.aa-badge--slate { color: #94A3B8; border-color: rgba(100, 116, 139, 0.35); background: rgba(100, 116, 139, 0.08); }

.aa-verdict {
  border: 1px solid rgba(52, 211, 153, 0.26); border-left: 2px solid var(--aa-emerald);
  border-radius: 0.5rem; background: rgba(52, 211, 153, 0.05);
  padding: 0.55rem 0.65rem 0.6rem; margin: 0.15rem 0 0.2rem;
}
.aa-verdict--rejected {
  border-color: rgba(251, 113, 133, 0.26); border-left-color: var(--aa-rose);
  background: rgba(251, 113, 133, 0.05);
}
.aa-verdict-row { display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap; }
.aa-verdict-source {
  margin-left: auto; font-family: var(--aa-mono); font-size: 0.53rem;
  letter-spacing: 0.08em; text-transform: uppercase; color: #475569;
}
.aa-verdict-reason {
  margin: 0.42rem 0 0; font-family: var(--aa-mono); font-size: 0.73rem !important;
  line-height: 1.55; color: var(--aa-text); overflow-wrap: anywhere;
}
.aa-clamp-note {
  margin-top: 0.42rem; border-top: 1px solid rgba(251, 191, 36, 0.18); padding-top: 0.38rem;
  font-size: 0.66rem; line-height: 1.5; color: #FCD34D;
}
.aa-clamp-label {
  font-family: var(--aa-mono); font-size: 0.53rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: #FBBF24; margin-right: 0.3rem;
}
.aa-mirror {
  margin-top: 0.42rem; border-top: 1px solid rgba(34, 211, 238, 0.2);
  padding-top: 0.38rem; font-size: 0.66rem !important; line-height: 1.5;
  color: #A5F3FC;
}
.aa-mirror .aa-clamp-label { color: var(--aa-cyan); }
.aa-mirror strong { font-weight: 650; }
.aa-mini-grid {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.45rem;
  margin-top: 0.55rem;
}
.aa-mini {
  min-width: 0; border: 1px solid rgba(31, 41, 55, 0.8); border-radius: 0.45rem;
  background: rgba(11, 18, 32, 0.5); padding: 0.42rem 0.5rem;
}
.aa-mini-value {
  margin: 0.12rem 0 0; font-family: var(--aa-mono); font-size: 0.84rem !important; font-weight: 650;
  color: var(--aa-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.aa-mini-hint { margin: 0.1rem 0 0; font-size: 0.58rem !important; color: var(--aa-label); line-height: 1.35; }
.aa-raw { margin-top: 0.55rem; }
.aa-raw-code {
  margin: 0.3rem 0 0; padding: 0.55rem 0.65rem; border: 1px solid var(--aa-border);
  border-radius: 0.45rem; background: #070C15; overflow-x: auto;
  font-family: var(--aa-mono); font-size: 0.64rem !important; line-height: 1.5; color: #A5B4FC;
  white-space: pre;
}
.aa-raw pre .aa-json-key { color: #7DD3FC; }
.aa-footnote { margin: 0.5rem 0 0; font-size: 0.64rem !important; line-height: 1.5; color: var(--aa-label); }
.aa-footnote code {
  font-family: var(--aa-mono); font-size: 0.62rem; color: #67E8F9;
}

/* --- Scorecards, empty states, thesis, tables -------------------------- */
.aa-score-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.55rem; }
.aa-score {
  border: 1px solid var(--aa-border); border-radius: 0.55rem; background: #0F172A;
  padding: 0.55rem 0.65rem;
}
.aa-score-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.aa-score-value {
  margin: 0.2rem 0 0; font-family: var(--aa-mono); font-size: 1.4rem !important; font-weight: 650;
  line-height: 1.3; color: #475569;
}
.aa-score-sub { margin: 0.1rem 0 0.32rem; font-size: 0.61rem !important; color: var(--aa-label); }
.aa-score-row {
  display: flex; justify-content: space-between; gap: 0.5rem;
  border-top: 1px solid rgba(31, 41, 55, 0.65); padding: 0.18rem 0;
  font-family: var(--aa-mono); font-size: 0.58rem; color: #475569;
}
.aa-score-row span:last-child { color: #64748B; }
.aa-empty {
  border: 1px dashed rgba(51, 65, 85, 0.8); border-radius: 0.55rem;
  background: rgba(11, 18, 32, 0.35); padding: 1.15rem 0.9rem; text-align: center;
}
.aa-empty-title {
  margin: 0; font-family: var(--aa-mono); font-size: 0.59rem !important; font-weight: 650;
  letter-spacing: 0.14em; text-transform: uppercase; color: #64748B;
}
.aa-empty-text { margin: 0.3rem auto 0; max-width: 27rem; font-size: 0.67rem !important; line-height: 1.5; color: #475569; }
.aa-thesis {
  border: 1px solid rgba(31, 41, 55, 0.9); border-radius: 0.55rem;
  background: #0B1220; padding: 0.62rem 0.72rem; margin-top: 0.5rem;
}
.aa-thesis-head { display: flex; align-items: baseline; gap: 0.5rem; margin-bottom: 0.32rem; }
.aa-thesis-ticker {
  font-family: var(--aa-mono); font-size: 0.76rem; font-weight: 650; color: var(--aa-text);
  letter-spacing: 0.03em;
}
.aa-thesis-held {
  font-family: var(--aa-mono); font-size: 0.53rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: #6EE7B7;
}
.aa-thesis-text { margin: 0; font-size: 0.71rem !important; line-height: 1.55; color: #CBD5E1; }
.aa-thesis-invalidation {
  margin: 0.45rem 0 0; border-top: 1px solid rgba(31, 41, 55, 0.72); padding-top: 0.4rem;
  font-size: 0.65rem !important; line-height: 1.5; color: #FCD34D;
}
.aa-thesis-invalidation span {
  font-family: var(--aa-mono); font-size: 0.53rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: #FBBF24; margin-right: 0.3rem;
}
.aa-table-wrap { overflow-x: auto; }
.aa-table { width: 100%; border-collapse: collapse; font-size: 0.65rem; }
.aa-table th {
  text-align: left; font-family: var(--aa-mono); font-size: 0.56rem !important; font-weight: 650;
  letter-spacing: 0.11em; text-transform: uppercase; color: #475569;
  border-bottom: 1px solid var(--aa-border); padding: 0.35rem 0.5rem;
}
.aa-table td {
  border-bottom: 1px solid rgba(31, 41, 55, 0.55); padding: 0.38rem 0.5rem;
  color: #94A3B8; vertical-align: top; font-size: 0.66rem !important;
}
.aa-table tr:last-child td { border-bottom: 0; }
.aa-table td code { font-family: var(--aa-mono); color: #CBD5E1; }
.aa-pipeline {
  display: block; margin: 0.4rem 0 0; padding: 0.6rem 0.7rem;
  border: 1px solid var(--aa-border); border-radius: 0.5rem; background: #070C15;
  overflow-x: auto; white-space: pre; font-family: var(--aa-mono);
  font-size: 0.63rem !important; line-height: 1.55; color: #94A3B8;
}
.aa-stat-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.55rem; }
.aa-stat {
  border: 1px solid var(--aa-border); border-radius: 0.55rem; background: #0F172A;
  padding: 0.55rem 0.62rem;
}
.aa-stat-value {
  margin: 0; font-family: var(--aa-mono); font-size: 1.3rem !important; font-weight: 650;
  line-height: 1.3; color: var(--aa-text);
}
.aa-stat-label {
  margin: 0.15rem 0 0; font-family: var(--aa-mono); font-size: 0.55rem !important;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--aa-label);
}
.aa-footer {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 0.4rem 1rem; margin-top: 0.9rem; padding-top: 0.7rem;
  border-top: 1px solid rgba(31, 41, 55, 0.8);
  font-family: var(--aa-mono); font-size: 0.53rem; letter-spacing: 0.1em;
  text-transform: uppercase; color: #475569;
}
.aa-footer a { color: #64748B; text-decoration: none; }
.aa-footer a:hover { color: var(--aa-emerald); }

/* --- Limits in force ---------------------------------------------------- */
.aa-limit-row {
  display: grid; grid-template-columns: 8.6rem minmax(0, 1fr); gap: 0.1rem 0.7rem;
  padding: 0.42rem 0 0.38rem; border-top: 1px solid rgba(31, 41, 55, 0.7);
}
.aa-limit-row:first-child { border-top: 0; padding-top: 0; }
.aa-limit-name {
  font-family: var(--aa-mono); font-size: 0.56rem !important; letter-spacing: 0.09em;
  text-transform: uppercase; color: #64748B; align-self: center;
}
.aa-limit-value {
  font-family: var(--aa-mono); font-size: 0.78rem !important; font-weight: 650;
  color: var(--aa-text); text-align: right; white-space: nowrap;
}
.aa-limit-hint {
  grid-column: 1 / -1; margin: 0; font-size: 0.61rem !important; line-height: 1.4;
  color: #64748B;
}

/* --- Side effects ------------------------------------------------------- */
.aa-effect-row {
  display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
  padding: 0.32rem 0; border-top: 1px solid rgba(31, 41, 55, 0.7);
}
.aa-effect-row:first-child { border-top: 0; padding-top: 0; }
.aa-effect-label {
  font-family: var(--aa-mono); font-size: 0.56rem !important; letter-spacing: 0.09em;
  text-transform: uppercase; color: #64748B;
}
.aa-effect-value {
  font-family: var(--aa-mono); font-size: 0.62rem !important; font-weight: 650;
  letter-spacing: 0.04em; text-transform: uppercase; color: #CBD5E1; text-align: right;
}
.aa-effect-value--emerald { color: var(--aa-emerald); }
.aa-effect-value--muted { color: #64748B; }

/* --- Native widgets ----------------------------------------------------- */
[data-testid="stWidgetLabel"] p {
  font-family: var(--aa-mono) !important; font-size: 0.61rem !important;
  letter-spacing: 0.06em !important; color: #94A3B8 !important;
}
[data-testid="stWidgetLabel"] { margin-bottom: 0.1rem; }
[data-testid="stSlider"] { padding-bottom: 0.2rem; }
[data-testid="stSlider"] [role="group"] > div {
  padding-top: 0.55rem !important; padding-bottom: 0.55rem !important;
}
[data-testid="stSlider"] div[data-rac][style*="left"] {
  background-color: var(--aa-emerald) !important;
  box-shadow: 0 0 0 2px rgba(52, 211, 153, 0.14) !important;
}
[data-testid="stSliderThumbValue"] { display: none !important; }
[data-testid="stWidgetLabel"] strong {
  color: var(--aa-emerald) !important; font-weight: 650 !important;
}
[data-testid="stSliderTickBar"] p {
  color: var(--aa-label) !important; font-family: var(--aa-mono) !important;
  font-size: 0.56rem !important;
}
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"] {
  background: var(--aa-panel-deep) !important; border: 1px solid var(--aa-border) !important;
  border-radius: 0.45rem !important;
}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within {
  border-color: rgba(52, 211, 153, 0.6) !important;
  box-shadow: 0 0 0 1px rgba(52, 211, 153, 0.2) !important;
}
[data-testid="stTextInputField"], [data-testid="stNumberInputField"] {
  background: transparent !important; color: var(--aa-text) !important;
  font-family: var(--aa-mono) !important; font-size: 0.8rem !important;
  border: 0 !important;
}
[data-testid="stNumberInputStepDown"], [data-testid="stNumberInputStepUp"] {
  background: var(--aa-panel) !important; color: #94A3B8 !important;
  border-left: 1px solid var(--aa-border) !important;
}
[data-testid="stRadioOption"] p {
  font-family: var(--aa-mono) !important; font-size: 0.68rem !important;
  color: #94A3B8 !important;
}
[data-testid="stRadio"] [role="radiogroup"] { gap: 0.4rem; }
[data-testid="stRadioOption"] > div > div:first-child {
  background: var(--aa-panel-deep) !important; border-color: #334155 !important;
}
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child {
  background: var(--aa-emerald) !important; border-color: var(--aa-emerald) !important;
}
[data-testid="stRadioOption"][data-selected="true"] > div > div:first-child > div {
  background: var(--aa-panel-deep) !important;
}
[data-testid="stTooltipIcon"] button { color: #475569 !important; }
[data-testid="stTooltipIcon"] button:hover { color: #94A3B8 !important; }
.stButton button, [data-testid="stBaseButton-secondary"] {
  background: rgba(11, 18, 32, 0.7) !important; border: 1px solid var(--aa-border) !important;
  border-radius: 0.42rem !important; color: #CBD5E1 !important; min-height: 2.15rem;
  transition: border-color 120ms ease, background 120ms ease;
}
.stButton button:hover, [data-testid="stBaseButton-secondary"]:hover {
  border-color: rgba(52, 211, 153, 0.45) !important;
  background: rgba(17, 24, 39, 0.95) !important; color: var(--aa-text) !important;
}
.stButton button p {
  font-family: var(--aa-mono) !important; font-size: 0.59rem !important;
  font-weight: 650 !important; letter-spacing: 0.07em !important;
  text-transform: uppercase !important; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
[data-testid="stCaptionContainer"] p {
  font-size: 0.67rem !important; color: var(--aa-label) !important; line-height: 1.5;
}

/* Custom HTML blocks do not end in a paragraph margin, so Streamlit's -16px
   markdown offset would let the following block start under their last line. */
[data-testid="stMarkdownContainer"]:has(.aa-topbar),
[data-testid="stMarkdownContainer"]:has(.aa-alert),
[data-testid="stMarkdownContainer"]:has(.aa-metrics),
[data-testid="stMarkdownContainer"]:has(.aa-meters),
[data-testid="stMarkdownContainer"]:has(.aa-meta),
[data-testid="stMarkdownContainer"]:has(.aa-section-head),
[data-testid="stMarkdownContainer"]:has(.aa-group-label),
[data-testid="stMarkdownContainer"]:has(.aa-panel),
[data-testid="stMarkdownContainer"]:has(.aa-card-grid),
[data-testid="stMarkdownContainer"]:has(.aa-verdict),
[data-testid="stMarkdownContainer"]:has(.aa-score-grid),
[data-testid="stMarkdownContainer"]:has(.aa-thesis),
[data-testid="stMarkdownContainer"]:has(.aa-table-wrap),
[data-testid="stMarkdownContainer"]:has(.aa-stat-grid),
[data-testid="stMarkdownContainer"]:has(.aa-footer) {
  margin-bottom: 0 !important;
}
[data-testid="stExpander"] details {
  border: 1px solid var(--aa-border) !important; border-radius: 0.5rem !important;
  background: rgba(11, 18, 32, 0.45) !important; overflow: hidden;
}
[data-testid="stExpander"] summary {
  font-family: var(--aa-mono) !important; font-size: 0.59rem !important;
  letter-spacing: 0.08em !important; text-transform: uppercase !important; color: #94A3B8 !important;
}
[data-testid="stExpander"] summary:hover { color: var(--aa-text) !important; }
[data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {
  font-size: 0.7rem; line-height: 1.55; color: var(--aa-muted);
}
[data-testid="stCode"] pre, [data-testid="stCodeBlock"] pre {
  background: #070C15 !important; border: 1px solid var(--aa-border) !important;
  border-radius: 0.5rem !important;
  font-family: var(--aa-mono) !important; font-size: 0.66rem !important;
}
[data-testid="stCode"] code, [data-testid="stCodeBlock"] code { color: #A5B4FC !important; }
[data-testid="stHorizontalBlock"] { gap: 0.75rem; }

/* --- Responsive --------------------------------------------------------- */
@media (max-width: 1180px) {
  .aa-metrics { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .aa-card-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .aa-stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 820px) {
  [data-testid="stMainBlockContainer"], .block-container { padding: 0.8rem 0.9rem 2.5rem; }
  .aa-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .aa-meters, .aa-score-grid, .aa-card-grid { grid-template-columns: minmax(0, 1fr); }
  .aa-mini-grid { grid-template-columns: minmax(0, 1fr); }
  .aa-brand-sub { letter-spacing: 0.14em; }
  .aa-verdict-source { display: none; }
}
@media (max-width: 460px) {
  .aa-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .aa-metrics .aa-metric:last-child { grid-column: 1 / -1; }
  .aa-pill--ghost { display: none; }
  [data-testid="stTab"] { padding: 0.5rem 0.35rem !important; }
  [data-testid="stTab"] p { font-size: 0.55rem !important; letter-spacing: 0.09em !important; }
}
@media (max-width: 360px) {
  .aa-metrics { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
}
</style>
"""


# ---------------------------------------------------------------------------
# HTML fragments
# ---------------------------------------------------------------------------
def _badge(label: str, tone: str = "slate") -> str:
    return f'<span class="aa-badge aa-badge--{tone}">{_e(label)}</span>'


def _bar(pct: Decimal | float | None, tone: str = "emerald", label: str = "") -> str:
    width = 0.0 if pct is None else max(0.0, min(100.0, float(pct)))
    return (
        f'<div class="aa-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
        f'aria-valuenow="{width:.0f}" aria-label="{_e(label)}">'
        f'<span class="aa-bar-fill aa-bar-fill--{tone}" style="width:{width:.1f}%"></span>'
        "</div>"
    )


def _metric(
    label: str,
    value: str,
    hint: str,
    *,
    tone: str = "",
    bar_pct: Decimal | float | None = None,
    bar_tone: str = "emerald",
) -> str:
    tone_class = f" aa-tone--{tone}" if tone else ""
    bar_html = _bar(bar_pct, bar_tone, f"{label} consumption") if bar_pct is not None else ""
    return (
        '<div class="aa-metric">'
        f'<p class="aa-label">{_e(label)}</p>'
        f'<p class="aa-metric-value{tone_class}">{_e(value)}</p>'
        f"{bar_html}"
        f'<p class="aa-metric-hint" title="{_e(hint)}">{_e(hint)}</p>'
        "</div>"
    )


def _section_head(title: str, caption: str, badge: str = "", tone: str = "slate") -> str:
    badge_html = _badge(badge, tone) if badge else ""
    return (
        '<div class="aa-section-head">'
        f'<div><div class="aa-panel-title" role="heading" aria-level="2">{_e(title)}</div>'
        f'<p class="aa-panel-caption">{_e(caption)}</p></div>'
        f"{badge_html}"
        "</div>"
    )


def _panel(
    title: str,
    caption: str,
    body: str,
    *,
    badge: str = "",
    tone: str = "slate",
) -> str:
    return (
        '<section class="aa-panel">'
        '<header class="aa-panel-head">'
        f'<div><div class="aa-panel-title" role="heading" aria-level="2">{_e(title)}</div>'
        f'<p class="aa-panel-caption">{_e(caption)}</p></div>'
        f"{_badge(badge, tone) if badge else ''}"
        "</header>"
        f'<div class="aa-panel-body">{body}</div>'
        "</section>"
    )


def _card(title: str, body: str, tone: str = "") -> str:
    return (
        f'<div class="aa-card {tone}">'
        f'<p class="aa-card-title">{_e(title)}</p>'
        f'<div class="aa-card-body">{body}</div>'
        "</div>"
    )


def _bullets(items: Sequence[str]) -> str:
    return '<ul class="aa-list">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _empty_state(title: str, text: str) -> str:
    return (
        '<div class="aa-empty">'
        f'<p class="aa-empty-title">{_e(title)}</p>'
        f'<p class="aa-empty-text">{_e(text)}</p>'
        "</div>"
    )


def _scorecard(horizon: str, note: str) -> str:
    return (
        '<div class="aa-score">'
        '<div class="aa-score-head">'
        f'<p class="aa-label">{_e(horizon)} horizon</p>'
        f'<span class="aa-badge aa-badge--slate">0 scored</span>'
        "</div>"
        '<p class="aa-score-value">—</p>'
        f'<p class="aa-score-sub">{_e(note)}</p>'
        '<div class="aa-score-row"><span>avg decision</span><span>—</span></div>'
        '<div class="aa-score-row"><span>avg alpha vs SPY</span><span>—</span></div>'
        '<div class="aa-score-row"><span>beat benchmark</span><span>—</span></div>'
        "</div>"
    )


def _stat(value: str, label: str, tone: str = "") -> str:
    tone_class = f" aa-tone--{tone}" if tone else ""
    return (
        '<div class="aa-stat">'
        f'<p class="aa-stat-value{tone_class}">{_e(value)}</p>'
        f'<p class="aa-stat-label">{_e(label)}</p>'
        "</div>"
    )


def _topbar() -> str:
    return """
<div class="aa-topbar">
  <div class="aa-brand">
    <div class="aa-logo" aria-hidden="true">&#945;</div>
    <div>
      <div class="aa-brand-name">AlphaAgent</div>
      <div class="aa-brand-sub">Autonomous Investment Terminal</div>
    </div>
  </div>
  <div class="aa-pills" aria-label="Terminal status">
    <span class="aa-pill aa-pill--live"><span class="aa-live-dot"></span>LIVE</span>
    <span class="aa-pill">demo · MODERATE</span>
    <span class="aa-pill aa-pill--cyan">OFFLINE · NO NETWORK</span>
    <span class="aa-pill aa-pill--ghost">GUARD · services/execution.py</span>
  </div>
</div>
"""


def _offline_alert() -> str:
    return """
<div class="aa-alert" role="note">
  <span class="aa-alert-mark" aria-hidden="true">[i]</span>
  <p><strong>Offline demo.</strong> No API keys, no database and no network calls from the demo code. Every verdict on
  this page is produced by the project's real guard,
  <code>services/execution.py::evaluate_proposal()</code> — the same function the Celery worker,
  REST API and Telegram bot call. Django is bootstrapped with <code>DATABASES={}</code> and the
  guard runs on unsaved model instances.</p>
</div>
"""


def _metrics_html(numbers: DashboardNumbers, inputs: GuardInputs, budget: Decimal) -> str:
    equity = numbers.equity
    cash_hint = (
        f"{numbers.equity_share_pct:.1f}% of equity"
        if numbers.equity_share_pct is not None
        else "Available buying power"
    )
    position_word = "position" if numbers.open_positions == 1 else "positions"
    unrealised_tone = (
        "emerald" if numbers.unrealised > 0 else "rose" if numbers.unrealised < 0 else "slate"
    )
    unrealised_hint = (
        f"{_pct(numbers.unrealised_pct, signed=True)} vs cost basis"
        if numbers.unrealised_pct is not None
        else "no open position"
    )
    realised_tone = (
        "emerald" if inputs.realised_pnl > 0 else "rose" if inputs.realised_pnl < 0 else "slate"
    )
    if numbers.loss_remaining is None:
        loss_value = "—"
        loss_hint = "kill switch disabled (limit $0.00)"
        loss_tone = "slate"
        loss_bar = 0
        loss_bar_tone = "slate"
    else:
        loss_value = _money(numbers.loss_remaining)
        loss_hint = f"{_money(numbers.loss_used)} of {_money(inputs.loss_limit)} consumed"
        loss_tone = "rose" if numbers.loss_ratio is not None and numbers.loss_ratio >= 1 else ""
        loss_bar = min(100.0, float(numbers.loss_ratio or 0) * 100)
        loss_bar_tone = "rose" if loss_tone == "rose" else "emerald"
    realised_hint = (
        f"kill switch {_money(-inputs.loss_limit)}"
        if inputs.loss_limit > 0
        else "kill switch disabled"
    )

    cards = [
        _metric(
            "Total Equity",
            _money(equity),
            f"Cost basis {_money(numbers.position_cost)}",
        ),
        _metric("Cash Balance", _money(inputs.cash), cash_hint),
        _metric(
            "Positions Value",
            _money(numbers.positions_value),
            f"{numbers.open_positions} {position_word}",
        ),
        _metric(
            "Unrealised P&L",
            _signed_money(numbers.unrealised),
            unrealised_hint,
            tone=unrealised_tone,
        ),
        _metric(
            "Realised P&L Today",
            _signed_money(inputs.realised_pnl),
            realised_hint,
            tone=realised_tone,
        ),
        _metric(
            "Daily Loss Budget",
            loss_value,
            loss_hint,
            tone=loss_tone,
            bar_pct=loss_bar,
            bar_tone=loss_bar_tone,
        ),
        _metric(
            "AI Spend Today",
            "$0.00",
            "no LLM calls · $0.00 spent",
            tone="slate",
            bar_pct=0,
            bar_tone="emerald",
        ),
    ]
    return f'<div class="aa-metrics">{"".join(cards)}</div>'


def _meter(
    label: str,
    used_pct: Decimal | None,
    ratio: Decimal | None,
    ceiling: Decimal | None,
    value_usd: Decimal,
    ceiling_pct: Decimal,
    *,
    leading: str = "",
) -> str:
    over = ratio is not None and ratio > 1
    tone = "rose" if over else ""
    if ceiling is None or ratio is None:
        value = "—"
        hint = (
            "Ceiling disabled · the guard applies only ceilings greater than zero"
            if ceiling_pct == 0
            else "No equity to measure the ceiling against"
        )
        bar_pct = 0.0
    else:
        value = f"{used_pct:,.1f}%" if used_pct is not None else "—"
        difference = value_usd - ceiling
        if difference > 0:
            tail = f'<span class="aa-over">over by {_money(difference)}</span>'
        else:
            tail = f'<span class="aa-ok">{_money(-difference)} headroom</span>'
        prefix = f"{_e(leading)} " if leading else ""
        hint = (
            f"{prefix}{_money(value_usd)} of {_money(ceiling)} ceiling "
            f"({_pct(ceiling_pct)} of equity) · {tail}"
        )
        bar_pct = min(100.0, float(ratio) * 100)
    tone_class = f" aa-tone--{tone}" if tone else ""
    return (
        '<div class="aa-meter">'
        '<div class="aa-meter-head">'
        f'<p class="aa-label">{_e(label)}</p>'
        f'<p class="aa-meter-value{tone_class}">{value}</p>'
        "</div>"
        f'{_bar(bar_pct, "rose" if over else "emerald", label)}'
        f'<p class="aa-meter-hint">{hint}</p>'
        "</div>"
    )


def _meters_html(numbers: DashboardNumbers, inputs: GuardInputs) -> str:
    exposure = _meter(
        "Exposure Ceiling",
        numbers.exposure_used_pct,
        numbers.exposure_ratio,
        numbers.exposure_ceiling,
        numbers.positions_value,
        inputs.exposure_pct,
    )
    concentration = _meter(
        "Concentration Ceiling",
        numbers.concentration_used_pct,
        numbers.concentration_ratio,
        numbers.concentration_ceiling,
        numbers.ticker_value,
        inputs.concentration_pct,
        leading=inputs.ticker,
    )
    return f'<div class="aa-meters">{exposure}{concentration}</div>'


def _meta_html(numbers: DashboardNumbers, inputs: GuardInputs, budget: Decimal) -> str:
    return f"""
<div class="aa-meta">
  <span><dt>Risk profile</dt><dd>MODERATE</dd></span>
  <span><dt>Max trade alloc</dt><dd>{_pct(inputs.allocation_pct)}</dd></span>
  <span><dt>Max trade budget</dt><dd>{_money(budget)}</dd></span>
  <span><dt>Open positions</dt><dd>{numbers.open_positions}</dd></span>
  <span><dt>Autonomy</dt><dd class="aa-meta-accent">GUARD ENGAGED</dd></span>
</div>
"""


def _decision_json(decision: GuardDecision) -> str:
    return json.dumps(
        {
            "approved": decision.approved,
            "reason": decision.reason,
            "action": decision.action,
            "amount": str(decision.amount),
            "price": None if decision.price is None else str(decision.price),
            "notional": str(decision.notional),
            "clamped": decision.clamped,
            "notes": decision.notes,
        },
        indent=2,
    )


def _mini(label: str, value: str, hint: str = "") -> str:
    hint_html = f'<p class="aa-mini-hint">{_e(hint)}</p>' if hint else ""
    return (
        '<div class="aa-mini">'
        f'<p class="aa-label">{_e(label)}</p>'
        f'<p class="aa-mini-value">{_e(value)}</p>'
        f"{hint_html}"
        "</div>"
    )


def _verdict_html(
    decision: GuardDecision,
    inputs: GuardInputs,
    budget: Decimal,
) -> str:
    approved = decision.approved
    state = "allowed" if approved else "rejected"
    badge = _badge("ALLOWED" if approved else "REJECTED", "emerald" if approved else "rose")
    if decision.clamped:
        badge += " " + _badge("CLAMPED", "amber")

    clamp_html = ""
    if decision.notes:
        note = " ".join(decision.notes)
        clamp_html = (
            '<div class="aa-clamp-note"><span class="aa-clamp-label">Clamp note</span>'
            f"{_e(note)}</div>"
        )

    mirror_html = ""
    if inputs.side == "BUY" and not approved:
        mirror = _mirror_sell(inputs)
        asymmetry = (
            " — de-risking stays available while the kill switch is active"
            if "daily loss limit" in decision.reason
            else ""
        )
        mirror_tone = "emerald" if mirror.approved else "rose"
        mirror_html = (
            '<div class="aa-mirror"><span class="aa-clamp-label">Mirror SELL</span>'
            "Same portfolio state, opposite side — the guard's own answer: "
            f'<strong class="aa-tone--{mirror_tone}">'
            f"{'ALLOWED' if mirror.approved else 'REJECTED'}</strong> — "
            f"{_e(mirror.reason)}{_e(asymmetry)}</div>"
        )

    requested = (inputs.amount * inputs.price).quantize(CENT)
    position_hint = (
        f"ceiling {_pct(inputs.exposure_pct)} of equity"
        if inputs.exposure_pct > 0
        else "ceiling disabled"
    )
    ticker_hint = (
        f"ceiling {_pct(inputs.concentration_pct)} of equity"
        if inputs.concentration_pct > 0
        else "ceiling disabled"
    )

    return f"""
<div class="aa-verdict aa-verdict--{state}" data-verdict="{state}" role="status">
  <div class="aa-verdict-row">
    {badge}
    <span class="aa-verdict-source">evaluate_proposal() · executed just now</span>
  </div>
  <p class="aa-verdict-reason">{_e(decision.reason)}</p>
  {clamp_html}
</div>
<div class="aa-mini-grid">
  {_mini("Per-trade budget", _money(budget))}
  {_mini("Requested notional", _money(requested))}
  {_mini("Approved notional", _money(decision.notional))}
</div>
<div class="aa-mini-grid">
  {_mini("Equity · cash + positions", _money(inputs.equity_usd))}
  {_mini("Positions · at market", _money(inputs.positions_value_usd), position_hint)}
  {_mini(inputs.ticker, _money(inputs.ticker_value_usd), ticker_hint)}
</div>
<p class="aa-footnote">Both portfolio ceilings are percentages of <strong>equity</strong>
(cash + positions at market), not of cash and not of the per-trade budget. A buy that does not
fit is sized down; only a buy with no headroom is refused.</p>
{mirror_html}
<div class="aa-raw">
  <p class="aa-label">Raw GuardDecision · the function's untouched return value</p>
  <div class="aa-raw-code">{_e(_decision_json(decision))}</div>
</div>
<p class="aa-footnote">Read from disk with <code>inspect.getsource</code> — the source is in the
About tab. No verdict on this page is written by hand.</p>
"""


def _limits_panel(
    numbers: DashboardNumbers,
    inputs: GuardInputs,
    budget: Decimal,
) -> str:
    """The rules the controls can reach, with their current thresholds.

    This is a *derived* read-out of the widget values, not a second verdict: the
    statuses below say what the limit is and how much headroom exists, while the
    verdict panel remains the guard's own return value.
    """
    if numbers.loss_remaining is None:
        kill_value = "DISABLED"
        kill_tone = "slate"
        kill_hint = "daily loss limit is $0.00, so the kill switch is not applied"
    elif numbers.loss_ratio is not None and numbers.loss_ratio >= 1:
        kill_value = "TRIPPED"
        kill_tone = "rose"
        kill_hint = (
            f"realised {_money(inputs.realised_pnl)} against a "
            f"{_money(-inputs.loss_limit)} limit · BUY risk is frozen"
        )
    else:
        kill_value = "ARMED"
        kill_tone = "emerald"
        kill_hint = (
            f"realised {_money(inputs.realised_pnl)} of a "
            f"{_money(-inputs.loss_limit)} limit"
        )

    def ceiling_row(
        name: str, ceiling: Decimal | None, used: Decimal, pct: Decimal, detail: str
    ) -> str:
        if ceiling is None:
            value = "—" if pct > 0 else "DISABLED"
            hint = (
                "ceilings are percentages of equity, and equity here is $0.00"
                if pct > 0
                else f"ceiling is {_pct(pct)} of equity, so the guard does not apply it"
            )
        else:
            difference = ceiling - used
            if difference >= 0:
                tail = f"{_money(difference)} headroom"
            else:
                tail = f'<span class="aa-over">over by {_money(-difference)}</span>'
            value = _money(ceiling)
            hint = f"{_pct(pct)} of {_money(numbers.equity)} equity · {tail}"
            if detail:
                hint = f"{detail} · {hint}"
        return (
            '<div class="aa-limit-row">'
            f'<span class="aa-limit-name">{_e(name)}</span>'
            f'<span class="aa-limit-value">{_e(value)}</span>'
            f'<p class="aa-limit-hint">{hint}</p>'
            "</div>"
        )

    rows = [
        (
            '<div class="aa-limit-row">'
            '<span class="aa-limit-name">Rule 3 · Per-trade</span>'
            f'<span class="aa-limit-value">{_money(budget)}</span>'
            f'<p class="aa-limit-hint">{_pct(inputs.allocation_pct)} of '
            f"{_money(inputs.cash)} cash — a percentage of cash, not of equity</p></div>"
        ),
        (
            '<div class="aa-limit-row">'
            '<span class="aa-limit-name">Rule 4 · SELL cap</span>'
            f'<span class="aa-limit-value">{_units(inputs.position_amount)} units</span>'
            f'<p class="aa-limit-hint">{_e(inputs.ticker)} position — a SELL cannot exceed it</p>'
            "</div>"
        ),
        (
            '<div class="aa-limit-row">'
            '<span class="aa-limit-name">Rule 5 · Kill switch</span>'
            f'<span class="aa-limit-value aa-tone--{kill_tone}">{kill_value}</span>'
            f'<p class="aa-limit-hint">{kill_hint}</p>'
            "</div>"
        ),
        ceiling_row(
            "Rule 6 · Exposure",
            numbers.exposure_ceiling,
            numbers.positions_value,
            inputs.exposure_pct,
            f"{_money(numbers.positions_value)} invested",
        ),
        ceiling_row(
            "Rule 7 · Concentration",
            numbers.concentration_ceiling,
            numbers.ticker_value,
            inputs.concentration_pct,
            f"{_e(inputs.ticker)} {_money(numbers.ticker_value)}",
        ),
    ]
    body = "".join(rows) + (
        '<p class="aa-footnote">Derived from the same widgets as the verdict; the '
        "verdict panel is still the guard's untouched return value.</p>"
    )
    return _panel(
        "Limits in Force",
        "The five rules these controls can reach · values from the widgets",
        body,
        badge="DERIVED",
        tone="cyan",
    )


def _effects_panel() -> str:
    """A compact statement of the demo's blast radius."""
    rows = [
        ("Proposal", "Your widgets", "emerald"),
        ("Verdict", "evaluate_proposal()", "emerald"),
        ("Order", "Not sent", "muted"),
        ("Ledger", "Not written", "muted"),
        ("LLM", "Not called", "muted"),
        ("Database", "Not opened", "muted"),
        ("Network", "Not used", "emerald"),
    ]
    body = "".join(
        '<div class="aa-effect-row">'
        f'<span class="aa-effect-label">{_e(label)}</span>'
        f'<span class="aa-effect-value aa-effect-value--{tone}">{_e(value)}</span>'
        "</div>"
        for label, value, tone in rows
    )
    body += (
        '<p class="aa-footnote">Streamlit\'s own telemetry, if enabled, is the '
        "framework's — not this app's.</p>"
    )
    return _panel(
        "Side Effects",
        "What this page touches · the guard is a pure function over the widgets",
        body,
        badge="OFFLINE",
        tone="cyan",
    )


# ---------------------------------------------------------------------------
# The tabs
# ---------------------------------------------------------------------------
def _render_guard_tab(
    inputs: GuardInputs,
    numbers: DashboardNumbers,
    decision: GuardDecision,
    budget: Decimal,
) -> None:
    with st.container(key="aa-presets"):
        st.markdown(
            _section_head(
                "Scenario Presets",
                "Pick a known guard path, then move the sliders. Verdicts are never hard-coded.",
                badge="REAL GUARD",
                tone="emerald",
            ),
            unsafe_allow_html=True,
        )
        for start in range(0, len(PRESETS), PRESETS_PER_ROW):
            preset_row = PRESETS[start : start + PRESETS_PER_ROW]
            for column, preset in zip(st.columns(len(preset_row), gap="small"), preset_row, strict=True):
                with column:
                    st.button(
                        preset.short,
                        key=f"preset::{preset.label}",
                        on_click=_apply_preset,
                        args=(preset,),
                        help=preset.expectation,
                        width="stretch",
                    )
        st.caption(
            "A preset fills every input below; the guard then runs on the next render. "
            "Hover a button for the exact scenario it proves."
        )
        with st.expander("What each preset demonstrates"):
            for preset in PRESETS:
                st.markdown(f"**{preset.label}** — {preset.expectation}")

    controls, verdict_column = st.columns([1.04, 0.96], gap="medium")

    with controls, st.container(key="aa-controls"):
        st.markdown(
            _section_head(
                "Portfolio Risk Configuration",
                "Every value below is an input to evaluate_proposal().",
            ),
            unsafe_allow_html=True,
        )
        st.markdown(
            '<p class="aa-group-label">Per-trade budget &amp; kill switch</p>',
            unsafe_allow_html=True,
        )
        st.slider(
            f"Cash balance (USD) · **{_money(inputs.cash)}**",
            min_value=0.0,
            max_value=100_000.0,
            step=500.0,
            key="cash",
            help="Portfolio.balance_usd — uninvested cash available to new positions.",
        )
        st.slider(
            f"Max trade allocation (% of cash) · **{_pct(inputs.allocation_pct)}**",
            min_value=0.0,
            max_value=25.0,
            step=0.25,
            key="allocation",
            help="Portfolio.max_trade_allocation_pct — the per-trade ceiling the guard enforces.",
        )
        st.slider(
            f"Daily loss limit (USD, kill switch) · **{_money(inputs.loss_limit)}**",
            min_value=0.0,
            max_value=5_000.0,
            step=50.0,
            key="loss_limit",
            help="Portfolio.daily_loss_limit_usd — once breached, new BUY risk is frozen.",
        )
        st.slider(
            f"Realised P&L today (USD) · **{_signed_money(inputs.realised_pnl)}**",
            min_value=-5_000.0,
            max_value=2_000.0,
            step=25.0,
            key="realised",
            help="Passed to the guard as realised_pnl_today (negative = losses).",
        )
        st.slider(
            f"Max portfolio exposure (% of equity) · **{_pct(inputs.exposure_pct)}**",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="exposure_pct",
            help=(
                "Portfolio.max_portfolio_exposure_pct — caps TOTAL invested capital as a "
                "percentage of equity (cash + positions at market), not of cash. 0 disables "
                "the limit: the guard only applies a ceiling greater than zero."
            ),
        )
        st.slider(
            f"Max ticker concentration (% of equity) · **{_pct(inputs.concentration_pct)}**",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key="concentration_pct",
            help=(
                "Portfolio.max_ticker_concentration_pct — caps ONE instrument, again as a "
                "percentage of equity. A portfolio 90% invested in a single stock passes the "
                "exposure limit and is not diversified at all, which is why this is a "
                "separate ceiling."
            ),
        )

        st.markdown(
            '<p class="aa-group-label">Existing positions · at market</p>',
            unsafe_allow_html=True,
        )
        st.number_input(
            "Units already held (drives SELL limits)",
            min_value=0.0,
            max_value=1_000_000.0,
            step=1.0,
            key="position",
            help=(
                "services.ledger.PositionState reconstructed from the ledger. It also values "
                "this instrument for the concentration ceiling (units held x price)."
            ),
        )
        st.slider(
            "Other holdings, at market (USD, excluding this ticker) · "
            f"**{_money(inputs.other_positions_value)}**",
            min_value=0.0,
            max_value=200_000.0,
            step=50.0,
            key="other_positions",
            help=(
                "Everything the portfolio holds apart from this ticker. With this ticker's "
                "holding it forms Exposure.positions_value_usd."
            ),
        )
        st.number_input(
            "Cost basis per unit (USD, display only)",
            min_value=0.01,
            max_value=1_000_000.0,
            step=1.0,
            key="cost_basis",
            help=(
                "Used only for the UNREALISED P&L card. The guard never receives this value: "
                "evaluate_proposal() has no cost basis in its contract. Other holdings are "
                "entered at market, so they are treated as carrying no unrealised P&L."
            ),
        )
        st.caption(
            "`Exposure` is built from these widgets: **ticker value** = units held x price, "
            "**positions** = that plus other holdings, **equity** = cash + positions. The guard "
            "never fetches these numbers; they are passed in, which keeps "
            "`evaluate_proposal` a pure function and this page free of market data."
        )

        st.markdown(
            '<p class="aa-group-label">The AI proposal · replaced by you</p>',
            unsafe_allow_html=True,
        )
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
            help=(
                "In production this comes from yfinance; the guard rejects any trade without "
                "a live price."
            ),
        )

    with verdict_column:
        with st.container(key="aa-verdict"):
            st.markdown(
                _section_head(
                    "Guard Verdict",
                    "The untouched return value of services.execution.evaluate_proposal().",
                    badge="LIVE",
                    tone="emerald",
                ),
                unsafe_allow_html=True,
            )
            st.markdown(_verdict_html(decision, inputs, budget), unsafe_allow_html=True)
        st.markdown(_limits_panel(numbers, inputs, budget), unsafe_allow_html=True)
        st.markdown(_effects_panel(), unsafe_allow_html=True)


def _render_quality_tab() -> None:
    st.markdown(
        _panel(
            "Decision Quality",
            "Outcome attribution · scored against SPY at 1, 7 and 30 days",
            '<div class="aa-alert" role="note"><span class="aa-alert-mark" aria-hidden="true">[!]</span>'
            "<p><strong>Not executed on this page.</strong> Scoring a decision needs the price "
            "history that followed it, so <code>services/attribution.py</code> reads "
            "<code>AgentDecisionLog</code> / <code>DecisionOutcome</code> rows and calls yfinance. "
            "This demo has no database and makes no network calls, so the module is "
            "<strong>not imported</strong>. The panels below describe the real implementation; "
            "the guard in the playground is the only part of the platform this page executes.</p></div>",
            badge="NOT EXECUTED",
            tone="rose",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Horizon Scorecards",
            "On the deployed dashboard these are read from DecisionOutcome via build_scorecard()",
            '<div class="aa-score-grid">'
            + _scorecard("1D", "nothing scored in the demo")
            + _scorecard("7D", "nothing scored in the demo")
            + _scorecard("30D", "nothing scored in the demo")
            + "</div>",
            badge="0 SCORED",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Recent Scored Decisions",
            "The individual decisions behind the scorecard · 25 most recent",
            _empty_state(
                "Nothing scored yet",
                "An outcome row is written once each decision's horizon has elapsed. On this "
                "page there is no ledger to read and no forward price series to read it against.",
            ),
            badge="NO DATABASE",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="aa-card-grid">'
        + _card(
            "Forward returns · 1 / 7 / 30 days",
            _bullets(
                [
                    "<code>DecisionOutcome.HORIZONS = (1, 7, 30)</code>: each scored decision "
                    "gets one row per horizon.",
                    "Scoring is idempotent — an existing (decision, horizon) pair is skipped, "
                    "so running the sweep more often costs only time.",
                    "The starting price is the transaction's own price whenever there is one, "
                    "not that day's close: measuring from the close would score the trade at a "
                    "price it never got.",
                ]
            ),
        )
        + _card(
            "Scored by the action, not the price",
            _bullets(
                [
                    "A BUY is right when the price rises; a <strong>SELL is right when the price "
                    "falls</strong> (<code>signed_return</code>). Scoring everything against "
                    "“did the price go up” would mark every correct exit as a failure.",
                    "A <strong>HOLD makes no directional claim</strong>: its "
                    "<code>directional_hit</code> is <code>None</code>, not <code>False</code>.",
                    "HOLDs are excluded from the hit rate rather than counted as misses; they "
                    "still carry a <code>decision_return_pct</code> of zero, so they stay visible "
                    "in the average.",
                ]
            ),
        )
        + _card(
            "SPY as the benchmark",
            _bullets(
                [
                    "The benchmark is <strong>SPY</strong>, recorded per decision; alpha is the "
                    "decision return minus the benchmark return over the same window.",
                    "A missing benchmark is <strong>null, never zero</strong> — an unknown "
                    "comparison is not a loss.",
                    "Scorecards are computed on read (<code>build_scorecard</code>), so a "
                    "re-evaluation shows up immediately; an empty scorecard reports a hit rate "
                    "of <code>None</code> rather than <code>0%</code>.",
                ]
            ),
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Why It Matters",
            "The audit trail and the scorecard answer different questions",
            "<p>The decision log answers <strong>why</strong> a decision was made; attribution "
            "answers <strong>whether it worked</strong>. Together they close the loop the "
            "platform was missing. This one needs a database and price history, which is why it "
            "is described here and not driven.</p>",
        ),
        unsafe_allow_html=True,
    )


def _render_thesis_tab() -> None:
    st.markdown(
        _panel(
            "Position Theses",
            "One durable paragraph per open position · written by the agent that opened it",
            '<div class="aa-alert" role="note"><span class="aa-alert-mark" aria-hidden="true">[!]</span>'
            "<p><strong>Not executed on this page.</strong> A thesis is a row in the "
            "<code>PositionThesis</code> table and <code>services/thesis.py</code> reads and writes "
            "it through the Django ORM, which needs a database. The module is "
            "<strong>not imported</strong> here — again a description of the real implementation, "
            "not a simulation of it.</p></div>",
            badge="NOT EXECUTED",
            tone="rose",
        ),
        unsafe_allow_html=True,
    )

    thesis_card = (
        '<div class="aa-thesis">'
        '<div class="aa-thesis-head">'
        '<span class="aa-thesis-ticker">AAPL</span>'
        '<span class="aa-thesis-held">HELD</span>'
        '<span class="aa-badge aa-badge--slate">ILLUSTRATIVE</span>'
        "</div>"
        '<p class="aa-thesis-text">Services margin expanding and the upgrade cycle is intact: '
        "buying a starter position.</p>"
        '<p class="aa-thesis-invalidation"><span>Would be wrong if</span>'
        "Gross margin below 40% for two consecutive quarters.</p>"
        "</div>"
    )
    st.markdown(
        _panel(
            "Position Theses · 1 Illustrative",
            "The card shape the dashboard renders next to the positions — not a row from this demo",
            thesis_card
            + '<p class="aa-footnote">The values above are an illustrative example of the stored '
            "shape, copied from the product's own screenshot. No <code>PositionThesis</code> is "
            "created, read or simulated by this page.</p>",
            badge="ILLUSTRATIVE",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Why Theses Exist",
            "The crew runs with memory=False (ADR-0007), so every sweep starts from nothing",
            "<p>The agents could sell at 10:00 and rebuy at 10:15 without noticing — not because "
            "they changed their mind, but because they had no record of having one. A thesis is "
            "the narrow fix: <strong>one durable paragraph per open position</strong>, written by "
            "the agent that opened it.</p>",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="aa-card-grid">'
        + _card(
            "Written on a BUY — and only a BUY",
            "<p>The CIO fills in <code>thesis</code> (why this is worth holding) and "
            "<code>invalidation</code> (the observable condition that would prove it wrong); a "
            "thesis that cannot be wrong is not a thesis. Both fields are optional on the "
            "proposal model, but a blank thesis is never recorded — a placeholder would be fed "
            "back to the agents as their own prior belief.</p>",
        )
        + _card(
            "Fed to the agents on every run",
            "<p>The live thesis arrives in the agents' brief as a "
            "<code>PRIOR THESIS (recorded when this position was opened)</code> block, with the "
            "stated invalidation condition. When there is none, the brief says so explicitly "
            "rather than leaving a blank section a model could fill in with an invented one.</p>",
        )
        + _card(
            "An exit has to argue with entry",
            "<p>When a prior thesis exists and the CIO proposes to SELL, the prompt requires it "
            "to state what has stopped being true — so a reversal is a change of mind by "
            "argument rather than by accident.</p>",
        )
        + _card(
            "Superseded, never edited",
            "<p>A new thesis on the same position closes the previous row "
            "(<code>close_reason=\"Superseded by a new thesis on the same position.\"</code>) "
            "instead of overwriting it, so the table reads as a history of what was believed and "
            "when.</p>",
        )
        + _card(
            "Closed with the position",
            "<p>A SELL that empties the position closes the live thesis; leaving it open would "
            "feed a stale belief into the next run as though it were current. "
            "<code>sync_after_trade()</code> is the single entry point that keeps the two in "
            "step.</p>",
        )
        + _card(
            "Visible outside the crew",
            "<p>The dashboard renders the open theses next to the positions, and "
            "<code>GET /api/portfolio/thesis/</code> returns them "
            "(<code>?include=closed</code> adds the superseded ones).</p>",
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Not Conversational Memory",
            "One paragraph per open position, not a growing transcript",
            "<p>This is not a conversational memory, which would grow without bound and make "
            "runs irreproducible (ADR-0007). It is one paragraph per open position that keeps a "
            "decision and the reason for it together.</p>",
        ),
        unsafe_allow_html=True,
    )


def _render_about_tab() -> None:
    st.markdown(
        _panel(
            "The Crew",
            "Three CrewAI agents in a sequential process · not executed on this page",
            '<div class="aa-card-grid">'
            + _card(
                "Bullish Research Analyst",
                "<p>Builds the strongest evidence-based <strong>bull case</strong> from the latest "
                "headlines and price action.</p>"
                '<ul class="aa-list"><li><code>NewsSentimentTool</code></li>'
                "<li><code>PriceHistoryTool</code></li></ul>",
                "aa-card--bull",
            )
            + _card(
                "Risk Assessor · Short Seller",
                "<p>Builds the <strong>bear case</strong> from independent evidence: leverage, "
                "liquidity, cash burn, valuation and technical breakdown.</p>"
                '<ul class="aa-list"><li><code>FinancialHealthTool</code></li>'
                "<li><code>PriceHistoryTool</code></li><li><code>NewsSentimentTool</code></li></ul>",
                "aa-card--bear",
            )
            + _card(
                "Chief Investment Officer",
                "<p><strong>Adjudicates</strong> between the two arguments and decides "
                "BUY / SELL / HOLD given the live price and the investor's risk profile.</p>"
                '<ul class="aa-list"><li><code>MarketPriceTool</code> (yfinance)</li></ul>',
                "aa-card--verdict",
            )
            + "</div>",
            badge="NOT EXECUTED",
            tone="rose",
        ),
        unsafe_allow_html=True,
    )

    pipeline = """news + prices ──▶ BULL CASE ─────┐
                                  ├──▶ CIO: {action, amount, sentiment, reasoning}
fundamentals ──▶ BEAR CASE ───────┘                │
                                                  ▼
                          pydantic TradeProposal    (contract validation #1)
                          CrewAI task guardrail     (contract validation #2)
                          evaluate_proposal()       ◀── the guard you just drove
                          apply_trade() ──▶ immutable Transaction ledger"""
    st.markdown(
        _panel(
            "The Pipeline",
            "One-way: the guard is the only gate between an AI proposal and real money",
            f'<div class="aa-pipeline">{_e(pipeline)}</div>'
            + _bullets(
                [
                    "<strong>No agent tool touches the database</strong> — every tool is "
                    "read-only and returns JSON.",
                    "<strong>HOLD never reaches the database</strong>, and neither does a "
                    "rejected proposal.",
                    "<strong>The guard is not the model's job</strong>: a hallucinated size, or "
                    "an overshoot past the per-trade ceiling, the portfolio exposure ceiling or "
                    "the concentration ceiling, is clamped or refused by deterministic "
                    "arithmetic, not by another LLM.",
                    "Without an LLM credential the platform falls back to "
                    "<code>HeuristicDecisionEngine</code>, a deterministic offline engine — which "
                    "is why this demo needs no API key.",
                ]
            ),
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Risk Guard · Rules 1-7",
            "The rules the widgets can reach, and the ones they cannot",
            "<p><strong>Enforced on this page.</strong></p>"
            + _bullets(
                [
                    "<strong>Rule 3 — per-trade allocation ceiling</strong> "
                    "(<code>max_trade_allocation_pct</code>, a percentage of <strong>cash</strong>): "
                    "presets 1-3.",
                    "<strong>Rule 4 — SELL cannot exceed the held position</strong>: preset 5.",
                    "<strong>Rule 5 — daily-loss kill switch</strong> (BUY only): preset 4, and "
                    "the mirrored SELL shows de-risking stays open while it is active.",
                    "<strong>Rule 6 — portfolio exposure ceiling</strong> "
                    "(<code>max_portfolio_exposure_pct</code>, a percentage of "
                    "<strong>equity</strong>): presets 6 and 8.",
                    "<strong>Rule 7 — single-instrument concentration ceiling</strong> "
                    "(<code>max_ticker_concentration_pct</code>, also a percentage of "
                    "<strong>equity</strong>): presets 7, 8 and 9.",
                ]
            )
            + "<p style=\"margin-top:0.55rem\"><strong>Why Exposure is always supplied.</strong> "
            "Rules 6 and 7 are skipped when the guard is handed no <code>exposure</code>, or when "
            "equity is zero (<code>Exposure.usable</code> is false). An earlier version of this "
            "page passed none and exercised only the per-trade half while looking complete. It now "
            "always builds one from the widgets:</p>"
            '<div class="aa-pipeline">ticker_value_usd    = units held x price            '
            "this instrument, at market\npositions_value_usd = that + other holdings         "
            "every holding, at market\nequity_usd          = cash + positions_value_usd    "
            "the denominator for both ceilings</div>"
            + "<p style=\"margin-top:0.55rem\">Both ceilings are percentages of that "
            "<strong>equity</strong> — not of cash, and not of the per-trade budget, which is a "
            "percentage of cash. A BUY that does not fit is <strong>sized down</strong> to what "
            "does, and the clamp note names the ceiling that bound it; only a BUY with no "
            "headroom at all is refused, and that refusal is the guard's shared <code>$0</code> "
            "“cannot be sized down” message rather than a rule-specific one.</p>"
            "<p><strong>Not reachable from these widgets.</strong> Rule 1 (the side selector "
            "offers BUY and SELL only, so a HOLD never becomes a proposal here), rule 2 (the price "
            "minimum is $0.01, so “no reliable market price” cannot be produced) and the "
            "BUY-versus-cash half of rule 4 (the per-trade ceiling is at most 25% of cash, so a "
            "clamped BUY always fits the balance).</p>",
        ),
        unsafe_allow_html=True,
    )

    st.markdown(
        _panel(
            "Offline by Construction",
            "What this page deliberately does not have",
            '<div class="aa-stat-grid">'
            + _stat("0", "API keys")
            + _stat("0", "Databases")
            + _stat("0", "Network calls", "emerald")
            + _stat("2", "Pinned dependencies")
            + "</div>"
            + "<p style=\"margin-top:0.6rem\">Django is configured with "
            "<code>DATABASES={}</code> and every model instance is unsaved, so a database "
            "connection would fail loudly rather than quietly succeed. The guard is a pure "
            "function over those objects; it has no I/O to fake. Streamlit's own telemetry, if "
            "enabled, is the framework's, not this app's.</p>",
        ),
        unsafe_allow_html=True,
    )

    rows = [
        ("services/execution.py", "EXECUTED", "evaluate_proposal() and Exposure — the real guard."),
        ("services/ledger.py", "EXECUTED", "PositionState, the real position dataclass."),
        (
            "core/models.py",
            "EXECUTED",
            "Portfolio — instantiated unsaved; max_trade_budget_usd is its real property.",
        ),
        (
            "services/portfolio_metrics.py",
            "NOT EXECUTED",
            "build_exposure() is how production derives the same values; here they come from widgets.",
        ),
        (
            "services/attribution.py",
            "NOT IMPORTED",
            "Reads DecisionOutcome rows and price history; described in Decision quality.",
        ),
        (
            "services/thesis.py",
            "NOT IMPORTED",
            "Reads and writes PositionThesis through the ORM; described in Position thesis.",
        ),
        (
            "ai_agent.py",
            "NOT EXECUTED",
            "Needs CrewAI + an LLM; the proposal payload is a two-field stand-in.",
        ),
        (
            "tasks.py",
            "NOT EXECUTED",
            "Celery + PostgreSQL; the caller that persists an approved decision.",
        ),
    ]
    table_rows = "".join(
        f"<tr><td><code>{_e(path)}</code></td>"
        f"<td>{_badge(status, 'emerald' if status == 'EXECUTED' else 'slate')}</td>"
        f"<td>{_e(role)}</td></tr>"
        for path, status, role in rows
    )
    st.markdown(
        _panel(
            "Files This Demo Actually Exercises",
            "What runs, what is only described, and why",
            '<div class="aa-table-wrap"><table class="aa-table">'
            "<thead><tr><th>Path</th><th>Role</th><th>Detail</th></tr></thead>"
            f"<tbody>{table_rows}</tbody></table></div>",
        ),
        unsafe_allow_html=True,
    )

    with st.expander(
        f"Proof · source of the function that produced these verdicts ({_display_path(GUARD_SOURCE)})"
    ):
        st.caption(
            "Read from disk at runtime with `inspect.getsource`, so this is exactly the code that "
            f"just ran. Module: `{evaluate_proposal.__module__}`, imported from "
            f"`{_display_path(Path(inspect.getsourcefile(evaluate_proposal) or GUARD_SOURCE))}`."
        )
        st.code(inspect.getsource(evaluate_proposal), language="python")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
_init_state()
inputs = _inputs_from_state()
decision, budget = run_guard(inputs)
numbers = _dashboard_numbers(inputs, _dec(st.session_state["cost_basis"]))

st.markdown(DASHBOARD_CSS + _slider_fill_css(), unsafe_allow_html=True)
st.markdown(_topbar(), unsafe_allow_html=True)
st.markdown(_offline_alert(), unsafe_allow_html=True)
st.markdown(_metrics_html(numbers, inputs, budget), unsafe_allow_html=True)
st.markdown(_meters_html(numbers, inputs), unsafe_allow_html=True)
st.markdown(_meta_html(numbers, inputs, budget), unsafe_allow_html=True)

tab_guard, tab_quality, tab_thesis, tab_about = st.tabs(
    ["Guard playground", "Decision quality", "Position thesis", "About"]
)

with tab_guard:
    _render_guard_tab(inputs, numbers, decision, budget)
with tab_quality:
    _render_quality_tab()
with tab_thesis:
    _render_thesis_tab()
with tab_about:
    _render_about_tab()

st.markdown(
    f"""
<div class="aa-footer">
  <span>AlphaAgent · autonomous multi-agent investment platform</span>
  <span><a href="{GITHUB_URL}">github.com/SergeyGer/AlphaAgent</a> · MIT</span>
  <span>Real guard · no database · no API keys</span>
</div>
""",
    unsafe_allow_html=True,
)
