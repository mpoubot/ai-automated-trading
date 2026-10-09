#!/usr/bin/env python3
"""
AURA portfolio/portfolio_additional_enforcement.py — Integration module
for this package's three new portfolio dimensions (macro-bucket
concentration, Greeks limits, capital-allocation validation throttle).

WHY THIS IS A SEPARATE DECISION, NOT A CHANGE TO `.344`
------------------------------------------------------------------------
`.344` has no public extension point for adding a dimension to ITS OWN
`EnforcementDecision` — it runs its own internal `_check_*` functions
sequentially and ORs them into one decision via a private `_finalize()`.
Per this project's explicit instruction, `.344` is not modified. This
module instead builds its OWN `EnforcementDecision` (the real class,
imported from `.344`, not a lookalike) out of this package's three new
dimensions, and composes with `.344`'s pre-trade gate the same way every
other independent gate in this repo already composes with `.44` — via
`.368`'s `combine_enforcement_check_fns()`, the `(symbol, direction,
quantity, decision) -> verdict-with-.allowed/.reasons` contract used
everywhere else (see `.368` itself, which already does exactly this for
the earnings-blackout gate).

TWO DIFFERENT COMPOSITION SHAPES, AND WHY
------------------------------------------------------------------------
This module exposes two very differently-shaped things on purpose:

  1. `evaluate_additional_portfolio_dimensions()` — produces a FULL
     `EnforcementDecision` covering ALL THREE new dimensions (macro
     buckets, Greeks, validation throttle). This is a PORTFOLIO-LEVEL
     snapshot read, meant to run on the same cadence `.344`'s own
     `evaluate_portfolio_enforcement()` does (e.g. once per cycle), and
     to be journaled exactly like `.344`'s own decision (see below) —
     not a pre-trade check for one candidate order.

  2. `build_additional_portfolio_check_fn()` — returns a 4-arg
     `(symbol, direction, quantity, decision) -> verdict` closure,
     gating TWO of the three dimensions: Greeks, and — UPDATED
     2026-10-09 per Martin's explicit directive — macro-bucket
     SAME-DIRECTION STACKING. The validation throttle remains
     deliberately NOT included here: it is, by its own design (see
     `validation_throttle.py`'s module docstring), a SIZING signal, not
     a pre-trade veto — it never belongs in a `.allowed`-returning
     check_fn at all. Macro-bucket CONCENTRATION (`exposure_ratio`,
     gated by `max_bucket_exposure_ratio`) also remains NOT included
     here — there is still no single "before this one order" projection
     defined for that dimension. But macro-bucket STACKING now has an
     exact, Martin-specified formula and a hard, non-configurable 0.15
     threshold (see `macro_buckets.py`'s module docstring and
     `evaluate_stacking_pretrade_direction()`), evaluated on the
     CURRENT portfolio state for the candidate symbol's direction —
     this is a well-defined, always-computable pre-trade check, so it
     is wired in via the new `macro_config` parameter. Pass
     `macro_config=None` (the default) to skip it entirely — e.g. for a
     caller that only wants the Greeks gate.

HOW THIS COMPOSES INTO THE SAME CHOKE POINT EVERY OTHER GATE USES
------------------------------------------------------------------------
    from aura_v05368_earnings_blackout_gate import combine_enforcement_check_fns
    from portfolio_additional_enforcement import build_additional_portfolio_check_fn

    additional_portfolio_check_fn = build_additional_portfolio_check_fn(
        snapshot_provider=lambda: current_snapshot,     # e.g. `.343`'s build_portfolio_snapshot()
        greeks_config=my_greeks_config,
        account_equity_lookup=lambda: current_equity_usd,
        macro_config=my_macro_bucket_config,             # optional -- adds the stacking-ratio pre-trade veto
    )
    combined_check_fn = combine_enforcement_check_fns(
        existing_check_fn,              # e.g. `.344`'s own enforcement check_fn, or `.368`'s earnings-blackout one
        additional_portfolio_check_fn,  # this module's Greeks pre-trade gate
    )
    # combined_check_fn is now exactly the enforcement_check_fn `.53.run_cycle`
    # (or any equivalent runner) already expects -- no change to that
    # runner, no change to `.344`.

HOW THE FULL DECISION GETS JOURNALED ALONGSIDE `.344`'S OWN
------------------------------------------------------------------------
`record_additional_portfolio_decision()` below wraps `.361`'s REAL
`record_decision()` (imported, not re-implemented), writing to the SAME
journal file `.344`'s own decision is already written to, tagged with
`context={"source": "additional_portfolio_dimensions"}` so a reader of
`read_journal()` can tell the two apart while both live in one append-
only history:

    from aura_v05361_portfolio_enforcement_journal import record_decision, DEFAULT_JOURNAL_PATH

    enforcement_decision = evaluate_portfolio_enforcement(...)            # .344's own
    additional_decision  = evaluate_additional_portfolio_dimensions(...)  # this module's
    record_decision(DEFAULT_JOURNAL_PATH, enforcement_decision)                                   # .344's own, untouched
    record_additional_portfolio_decision(DEFAULT_JOURNAL_PATH, additional_decision)                # this module's, same file

`decision_hash` on the `EnforcementDecision` this module builds is
computed with `.344`'s OWN real `stable_json`/`sha256_text` functions
(imported, not re-derived), over the exact same
`{as_of, snapshot_as_of, snapshot_state_hash, overall_verdict,
dimension_verdicts}` body shape `.344`'s own private `_finalize()` uses
— so the hashing convention is not an approximation of `.344`'s, it is
`.344`'s, applied here.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ------------------------------------------------------------------------
# Loader (duplicated per-file by this package's own convention -- see
# macro_buckets.py's module docstring for why), plus this module's own
# sibling-package imports and the real `.361` journal import.
# ------------------------------------------------------------------------


_REQUIRED_CORE_FILES: tuple[str, ...] = (
    "aura_v05344_portfolio_exposure_enforcement.py",
    "aura_v05361_portfolio_enforcement_journal.py",
)


def _find_aura_core_dir() -> Path:
    """HARDENED 2026-10-09 per Martin's explicit directive -- validates
    that every required core file (both `.344` and `.361`, since this
    module loads both) is actually present in the resolved directory,
    not just that the directory exists (see macro_buckets.py's fuller
    docstring for the rationale; identical logic, kept duplicated per
    this package's own convention)."""
    env = os.environ.get("AURA_CORE_DIR")
    if env:
        candidate = Path(env)
        if not candidate.exists():
            raise RuntimeError(f"AURA_CORE_DIR={env!r} does not exist")
        missing = [f for f in _REQUIRED_CORE_FILES if not (candidate / f).is_file()]
        if missing:
            raise RuntimeError(
                f"AURA_CORE_DIR={env!r} exists but is missing required core module "
                f"file(s): {', '.join(missing)}. Point AURA_CORE_DIR at the directory "
                f"that directly contains the real aura_v053NN modules."
            )
        return candidate
    fallback = Path("/mnt/user-data/uploads/AI automated trading/aura-autonomous-trading-agent")
    if fallback.exists() and all((fallback / f).is_file() for f in _REQUIRED_CORE_FILES):
        return fallback
    raise RuntimeError(
        "Could not locate the aura_v053NN core module directory. Set the "
        "AURA_CORE_DIR environment variable to the directory containing "
        "aura_v05344_portfolio_exposure_enforcement.py and its siblings."
    )


def _load_module_from_file(module_name: str, file_path: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_CORE_DIR = _find_aura_core_dir()
_ENF = _load_module_from_file(
    "aura_v05344_portfolio_exposure_enforcement",
    _CORE_DIR / "aura_v05344_portfolio_exposure_enforcement.py",
)
_JOURNAL = _load_module_from_file(
    "aura_v05361_portfolio_enforcement_journal",
    _CORE_DIR / "aura_v05361_portfolio_enforcement_journal.py",
)

OBS = _ENF.OBS
PositionRecord = OBS.PositionRecord
PortfolioSnapshot = OBS.PortfolioSnapshot
DimensionVerdict = _ENF.DimensionVerdict
EnforcementDecision = _ENF.EnforcementDecision
PASS = _ENF.PASS
BLOCK = _ENF.BLOCK
LIMIT_NOT_CONFIGURED = _ENF.LIMIT_NOT_CONFIGURED
NOT_COMPUTABLE = _ENF.NOT_COMPUTABLE
stable_json = _ENF.stable_json
sha256_text = _ENF.sha256_text

record_decision = _JOURNAL.record_decision  # real .361 function, re-exported for convenience.

# Sibling modules in this same package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import macro_buckets  # noqa: E402
import validation_throttle  # noqa: E402
import greeks_limits  # noqa: E402


# ------------------------------------------------------------------------
# evaluate_additional_portfolio_dimensions
# ------------------------------------------------------------------------

def evaluate_additional_portfolio_dimensions(
    snapshot: PortfolioSnapshot,
    *,
    macro_config: "macro_buckets.MacroBucketConfig",
    greeks_config: "greeks_limits.GreeksLimitsConfig",
    validation_records: dict[str, "validation_throttle.TrackValidationRecord"],
    validation_config: "validation_throttle.ValidationThrottleConfig",
    account_equity_usd: float,
    now: datetime | None = None,
) -> EnforcementDecision:
    """EFFECTIVE. Evaluates all three new dimensions against the CURRENT
    `snapshot` and concatenates every `DimensionVerdict` into one REAL
    `EnforcementDecision` (`.344`'s own class). `overall_verdict` is
    `"BLOCK"` iff any dimension verdict is `BLOCK` — in practice, only
    the macro-bucket and Greeks dimensions can ever produce one; the
    validation-throttle dimension is always `PASS` by design (see
    `validation_throttle.py`). Pure function of its inputs plus `now`,
    matching `.344`'s own `evaluate_portfolio_enforcement()` contract."""
    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat()

    verdicts: list[DimensionVerdict] = []
    verdicts.extend(macro_buckets.evaluate_macro_bucket_dimension(
        snapshot, macro_config, account_equity_usd=account_equity_usd, now=now,
    ))
    verdicts.extend(greeks_limits.evaluate_greeks_dimension(
        snapshot, greeks_config, account_equity_usd=account_equity_usd, now=now,
    ))
    verdicts.extend(validation_throttle.evaluate_validation_throttle_dimension(
        validation_records, validation_config,
    ))

    overall = BLOCK if any(v.verdict == BLOCK for v in verdicts) else "ALLOW"
    body = {
        "as_of": as_of,
        "snapshot_as_of": snapshot.as_of,
        "snapshot_state_hash": snapshot.state_hash,
        "overall_verdict": overall,
        "dimension_verdicts": [v.to_dict() for v in verdicts],
    }
    decision_hash = sha256_text(stable_json(body))
    return EnforcementDecision(
        as_of=as_of,
        snapshot_as_of=snapshot.as_of,
        snapshot_state_hash=snapshot.state_hash,
        overall_verdict=overall,
        dimension_verdicts=tuple(verdicts),
        decision_hash=decision_hash,
    )


def record_additional_portfolio_decision(
    journal_path: Path, decision: EnforcementDecision, *, extra_context: dict[str, Any] | None = None, now: datetime | None = None,
):
    """Convenience wrapper over `.361`'s REAL `record_decision()` (see
    module docstring). Always tags `context["source"] =
    "additional_portfolio_dimensions"` so entries from this module are
    distinguishable, in the same journal file, from `.344`'s own
    entries -- `extra_context`, if given, is merged in on top (and may
    override `source` if a caller has a specific reason to)."""
    context: dict[str, Any] = {"source": "additional_portfolio_dimensions"}
    if extra_context:
        context.update(extra_context)
    return record_decision(journal_path, decision, context=context, now=now)


# ------------------------------------------------------------------------
# build_additional_portfolio_check_fn -- the pre-trade Greeks gate.
# ------------------------------------------------------------------------

@dataclass(frozen=True)
class AdditionalPortfolioCheckVerdict:
    """Shaped to satisfy `.368.combine_enforcement_check_fns()`'s duck-
    typed `.allowed`/`.reasons` contract."""
    allowed: bool
    reasons: tuple[str, ...]
    dimension_verdicts: tuple[DimensionVerdict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "dimension_verdicts": [v.to_dict() for v in self.dimension_verdicts],
        }


_OPEN_DIRECTIONS = frozenset({"OPEN_LONG", "OPEN_SHORT"})


def _build_hypothetical_option_position(
    symbol: str, direction: str, quantity: Any, decision: Any, *, now: datetime,
) -> PositionRecord | None:
    """Best-effort construction of a `.343` `PositionRecord` for the
    candidate order, from exactly the 4 arguments the `enforcement_
    check_fn` contract supplies. APPROXIMATED, documented explicitly:

      - `option_detail` is read from `decision.option_detail` (attribute
        or dict key) — this is NOT guaranteed to exist on every caller's
        `decision` object; the generic 4-arg contract carries no
        required option-Greeks shape. If absent, this function returns
        `None` and the check_fn below treats the order as not gate-able
        here (allowed, with a reason noting the skip) rather than
        guessing Greeks that were never supplied.
      - `mark_price` is read from `option_detail["mark_price"]` if
        present, else from `decision.mark_price`, else left `None`
        (which then makes `net_delta_usd` for this leg specifically
        `NOT_COMPUTABLE`-excluded inside `aggregate_portfolio_greeks()`
        — never silently assumed).
      - `entry_price` is approximated as `mark_price` (or `0.0` if that
        too is unavailable) since the 4-arg contract has no real fill
        price yet for a trade that has not executed — this field is not
        used by the Greeks aggregation this function exists to feed, but
        `PositionRecord` requires it.
      - `venue` is read from `decision.venue` if present, else defaults
        to `"ALPACA"` (options are Alpaca-only in this repo's current
        scope — see `.373`/`.376`) — also approximated, documented here
        rather than silently assumed without comment.
    """
    option_detail = getattr(decision, "option_detail", None)
    if option_detail is None and isinstance(decision, dict):
        option_detail = decision.get("option_detail")
    if not isinstance(option_detail, dict):
        return None

    mark_price = option_detail.get("mark_price")
    if mark_price is None:
        mark_price = getattr(decision, "mark_price", None)
        if mark_price is None and isinstance(decision, dict):
            mark_price = decision.get("mark_price")

    venue = getattr(decision, "venue", None)
    if venue is None and isinstance(decision, dict):
        venue = decision.get("venue")
    venue = venue or "ALPACA"

    pos_direction = "LONG" if direction == "OPEN_LONG" else "SHORT"
    entry_price = float(mark_price) if mark_price is not None else 0.0

    return PositionRecord(
        venue=venue,
        symbol=symbol,
        direction=pos_direction,
        quantity=float(quantity),
        entry_price=entry_price,
        leverage=None,
        mark_price=float(mark_price) if mark_price is not None else None,
        notional_usd=None,
        notional_basis=None,
        unrealized_pnl_usd=None,
        liquidation_price=None,
        raw_source_id=None,
        as_of=now.isoformat(),
        option_detail={
            k: option_detail[k] for k in ("strike", "expiry", "right", "delta", "gamma", "vega", "multiplier")
            if k in option_detail
        },
        structure_group_id=option_detail.get("structure_group_id"),
    )


def build_additional_portfolio_check_fn(
    snapshot_provider: Callable[[], PortfolioSnapshot],
    *,
    greeks_config: "greeks_limits.GreeksLimitsConfig",
    account_equity_lookup: Callable[[], float],
    macro_config: "macro_buckets.MacroBucketConfig | None" = None,
    now_fn: Callable[[], datetime] | None = None,
    mark_price_cache: "greeks_limits.MarkPriceCache | None" = None,
    underlying_price_provider: "greeks_limits.UnderlyingPriceProvider | None" = None,
    quote_provider: "greeks_limits.QuoteProvider | None" = None,
) -> Callable[[str, str, Any, Any], AdditionalPortfolioCheckVerdict]:
    """Returns a closure matching the `(symbol, direction, quantity,
    decision) -> verdict` `enforcement_check_fn` contract used
    everywhere else in this repo (see `.368`'s own earnings-blackout
    closure for the exact same shape). Gates the Greeks dimension, and —
    UPDATED 2026-10-09 per Martin's explicit directive — the macro-
    bucket same-direction stacking dimension when `macro_config` is
    supplied (see module docstring for why macro-bucket CONCENTRATION
    and validation-throttle remain excluded).

    `mark_price_cache`/`underlying_price_provider`/`quote_provider`
    (Task 2, 2026-10-09): pass straight through to `greeks_limits.
    would_breach_with_new_position()` — the mark-price fallback and
    wide-spread handling mechanism described in that module's
    docstring. All three default to `None`, reproducing this
    function's pre-2026-10-09 behavior exactly (a leg with no usable
    `mark_price` is excluded from the Greeks check, with no fallback
    attempted).

    For a non-`OPEN_LONG`/`OPEN_SHORT` direction (exits are never
    gated, matching `.368`'s own earnings-blackout convention), this is
    a no-op ALLOW. If `macro_config` is `None` AND `decision` carries no
    `option_detail` this function can build a hypothetical position
    from, this check is a no-op ALLOW with a reason explaining why —
    never a silent skip. Otherwise, whichever of the two gates has
    enough information to run, runs; a symbol that matches no configured
    macro bucket AND carries no `option_detail` still produces no
    verdicts (nothing to gate), but a symbol that matches a bucket is
    gated on stacking even with no `option_detail` at all, since the
    stacking check needs no options-specific data."""

    def check(symbol: str, direction: str, quantity: Any, decision: Any) -> AdditionalPortfolioCheckVerdict:
        now = now_fn() if now_fn is not None else datetime.now(timezone.utc)

        if direction not in _OPEN_DIRECTIONS:
            return AdditionalPortfolioCheckVerdict(allowed=True, reasons=(), dimension_verdicts=())

        proposed = _build_hypothetical_option_position(symbol, direction, quantity, decision, now=now)

        if macro_config is None and proposed is None:
            return AdditionalPortfolioCheckVerdict(
                allowed=True,
                reasons=("GREEKS_CHECK_SKIPPED:no option_detail supplied on decision for this candidate order",),
                dimension_verdicts=(),
            )

        snapshot = snapshot_provider()
        account_equity_usd = account_equity_lookup()
        verdicts: list[DimensionVerdict] = []

        if macro_config is not None:
            verdicts.extend(macro_buckets.evaluate_stacking_pretrade_direction(
                snapshot, macro_config, symbol, direction, account_equity_usd=account_equity_usd,
            ))
        if proposed is not None:
            verdicts.extend(greeks_limits.would_breach_with_new_position(
                snapshot, proposed, greeks_config, account_equity_usd=account_equity_usd,
                now=now, mark_price_cache=mark_price_cache,
                underlying_price_provider=underlying_price_provider, quote_provider=quote_provider,
            ))

        blocked = [v for v in verdicts if v.verdict == BLOCK]
        allowed = not blocked
        reasons = tuple(f"{v.dimension}:{v.reason}" for v in blocked)
        return AdditionalPortfolioCheckVerdict(allowed=allowed, reasons=reasons, dimension_verdicts=tuple(verdicts))

    return check
