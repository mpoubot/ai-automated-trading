# AURA Track B — Stage 3C Windows Execution Procedure

**Status: PREPARED, NOT YET RUN.** This document is the procedure only. Nothing in
it has been executed. Per Martin's instruction, this file is not committed or
pushed — it exists locally in the sandbox and is being handed to Martin directly.

**Purpose of Stage 3C:** the single, minimal real-world test of Stage 3A's
wiring — `aura_v05356_stage3_live_equity_cli.py` — against live Alpaca paper
data. One symbol, real market data, zero order submission. It verifies that
real data acquisition and the complete `.51/.52 → .53/.55 → .44` path work
end to end outside of fakes/mocks. It does **not** attempt to prove the
system can trade profitably, and it will **not** produce a live trade
decision — see "Expected output" below.

**Why this runs on Martin's machine, not the sandbox:** the sandbox's network
proxy blocks both `paper-api.alpaca.markets` and `data.alpaca.markets`
(confirmed via `curl` and the proxy's own status endpoint during the Stage 3
entry audit) — the same class of block seen earlier for MEXC and for
`git push`. This is an infrastructure boundary, not a code issue.

---

## 0. Frozen-parameter reminder — read this first

`FROZEN_DECIDE_KWARGS` (in `aura_v054_signal_source.py`, frozen 2026-09-15,
reused verbatim by the Stage 3A CLI) sets `technical_weight=0.0` and
`short_technical_weight=0.0`. This makes `.50.decide()` mathematically
**forced to ABSTAIN** for every symbol, every cycle, regardless of what the
real `.51`/`.52` signal computes from live data — this is proven by
`tests/test_aura_v054_signal_source.py` and documented in that module's own
docstring.

**Expected outcome of this first run: `decision_outcome: "ABSTAIN"` for the
one symbol tested, `submitted_count: 0`.** That is success, not failure — it
means real data came in, real `.51`/`.52` regimes were computed from it, the
existing decision engine ran on real inputs, and `.44` enforcement was
exercised, all without producing a trade (because the frozen weights make
that outcome impossible right now, by design, not by bug). Proving `.51`/
`.52` can actually drive a decision requires a separate, explicit decision
from Martin to reweight `technical_weight`/`short_technical_weight` — this
run does not do that, and this procedure does not touch those values.

Do not treat an ABSTAIN result as a sign something is broken. Do not adjust
`FROZEN_DECIDE_KWARGS`, `FROZEN_TECHNICAL_PARAMS`, or
`FROZEN_SHORT_TECHNICAL_PARAMS` to try to force a non-ABSTAIN result.

---

## 1. Environment setup (Windows)

1. Confirm the repo is up to date and `main` is at `4b33423` (or later, if
   more Track B commits have been pushed since):
   ```powershell
   cd path\to\ai-automated-trading
   git fetch origin
   git status
   git log --oneline -3
   ```
2. Confirm Python 3.11+ is available:
   ```powershell
   python --version
   ```
3. Create/activate a virtual environment (recommended — keeps this isolated
   from any other Python setup on the machine):
   ```powershell
   cd aura-autonomous-trading-agent
   python -m venv .venv
   .venv\Scripts\activate
   ```
4. Install dependencies (from the repo's own `requirements.txt` — nothing
   extra needed for Stage 3C):
   ```powershell
   pip install -r requirements.txt
   ```
   This installs `alpaca-py>=0.44.0`, `python-dotenv>=1.0.0`,
   `pandas>=2.0.0`, `numpy>=2.0.0`.

## 2. Credentials

Two environment variables, and **only** these two — the CLI never falls back
to the crypto-account pair (`ALPACA_PAPER_API_KEY`/`SECRET_KEY`) used
elsewhere in this repo:

```
ALPACA_EQUITY_PAPER_API_KEY
ALPACA_EQUITY_PAPER_SECRET_KEY
```

These must be a **paper-trading** key pair from an Alpaca account (an equity/
ETF-enabled paper account — a crypto-only paper account will not have market
data access for stocks). Do not use a live-trading key pair for this test.

Set them for the session (PowerShell) — do not put them in a file that gets
committed; `.env` is already git-ignored in this repo:

```powershell
$env:ALPACA_EQUITY_PAPER_API_KEY = "<your paper API key ID>"
$env:ALPACA_EQUITY_PAPER_SECRET_KEY = "<your paper secret key>"
```

Or, if preferred, create `aura-autonomous-trading-agent\.env` (gitignored)
from `.env.example` and fill in just those two lines — but note the CLI
itself reads `os.environ` directly, so if using a `.env` file you need
`python-dotenv` to load it into the process first (e.g. `pip install
python-dotenv` is already in requirements.txt; either `dotenv run` the
command below, or `Get-Content .env | ForEach-Object { ... }` to export it
into the shell — whichever Martin is already comfortable with for this repo).

I am not asking for and should not be sent the actual key values — this
procedure only names the variables.

## 3. Symbol-requests input file

Stage 3A takes no default symbol universe — it must be given an explicit
JSON file. Create a minimal one-symbol file for this first test, e.g.
`aura-autonomous-trading-agent\stage3c_one_symbol.json`:

```json
[
  {"symbol": "AAPL", "asset_class": "STOCK", "quantity": 1}
]
```

`AAPL` is a reasonable, liquid, widely-covered default for a first
connectivity test — swap it for any other S&P100-universe symbol Martin
prefers. `quantity` here is the hypothetical order size the dry-run preview
would use if a decision were ever non-ABSTAIN; it has no effect on the
ABSTAIN outcome expected in step 0.

## 4. The command

From `aura-autonomous-trading-agent\` (venv activated, credentials set):

```powershell
python aura_v05356_stage3_live_equity_cli.py `
  --requests-config stage3c_one_symbol.json `
  --max-new-orders-per-cycle 1 `
  --max-snapshot-age-seconds 300 `
  --output stage3c_output\one_symbol_result.json
```

Notes on the flags:
- `--max-new-orders-per-cycle 1` — irrelevant to the ABSTAIN outcome, but
  required (no default) by design; `1` is a safe, minimal value for a
  single-symbol test.
- `--max-snapshot-age-seconds 300` — how stale a portfolio snapshot `.44`
  will tolerate before blocking; `300` (5 minutes) is generous for an
  interactive one-off run. Required, no default, by `.44`'s own freshness
  discipline.
- `--lookback-bars` and `--universe-version` are omitted deliberately, so
  they default to the frozen values (`FROZEN_TECHNICAL_PARAMS.
  min_bars_required` = 55 bars, and `aura_v054_signal_source.
  UNIVERSE_VERSION` = `S&P100_FROZEN_2026-08-26_CORRECTED`) rather than
  being invented here.
- Do **not** pass `--skip-account-equity-fetch` for this run — the point is
  to exercise the real read-only `get_account()` path into `.44` as well.
  (If Martin wants a run that makes zero broker contact of any kind
  including account reads, that flag exists for that — but it's not what
  this first test is for.)
- `--output` writes a JSON result file. No flag or environment variable in
  this CLI can cause an order to be submitted — see the module's own
  docstring ("STRUCTURAL SAFETY, NOT A CONVENTION") for the structural
  argument, already verified by the Stage 3B test suite.

## 5. Expected output

On success, stdout prints one line:

```
Wrote stage3c_output\one_symbol_result.json (submitted_count=0, always 0 -- see module docstring)
```

The written JSON file should show (approximately):

```json
{
  "engine": "STAGE3A_LIVE_EQUITY_CLI",
  "version": "AURA v0.5.3.56",
  "observed_at": "<real UTC timestamp of the run>",
  "universe_version": "S&P100_FROZEN_2026-08-26_CORRECTED",
  "lookback_bars": 55,
  "technical_weight_is_zero_warning": "FROZEN_DECIDE_KWARGS.technical_weight == 0.0 and short_technical_weight == 0.0 -- ...",
  "account_equity_usd_source": "REAL_ALPACA_GET_ACCOUNT",
  "account_equity_usd": <a real number from the paper account>,
  "symbol_fetch_failures": [],
  "stage1_report": {
    "stage": "STAGE_1A_DRY_RUN",
    "submitted_count": 0,
    "outcomes": [
      {
        "symbol": "AAPL",
        "decision_outcome": "ABSTAIN",
        "reasons": [...]
      }
    ],
    "audit_records": [ ... ]
  }
}
```

If the exit code is `1` and stderr shows a line starting `FAIL-CLOSED:`,
that means the CLI refused to proceed rather than doing something
unverified — read the message (it will name exactly what's missing:
credentials, the requests file, etc.) and fix that before retrying. This is
expected, correct behavior for any misconfiguration, not a bug.

## 6. Safety checks before and after running

**Before:**
- [ ] Confirm the key pair is explicitly a **paper** account key (Alpaca
  paper and live keys look similar — verify in the Alpaca dashboard which
  account the key belongs to before using it).
- [ ] Confirm `ALPACA_EQUITY_PAPER_API_KEY`/`SECRET_KEY` are set, and that
  `ALPACA_PAPER_API_KEY`/`SECRET_KEY` (the crypto pair) are *not* being
  confused with them — the CLI won't fall back, but a copy-paste into the
  wrong variable name would just cause a clean `FAIL-CLOSED` on missing
  credentials, not a silent wrong-account run.
- [ ] Confirm Track A/MEXC files are untouched (`git status` should show
  nothing under `mexc_bot/` changing from this work).

**After:**
- [ ] Confirm stdout shows `submitted_count=0`.
- [ ] Open the output JSON and confirm `"submitted_count": 0` and
  `"decision_outcome": "ABSTAIN"` for the tested symbol.
- [ ] Confirm `account_equity_usd_source` is `"REAL_ALPACA_GET_ACCOUNT"` and
  `account_equity_usd` is a plausible real number for the paper account (not
  null/zero unless the account genuinely has zero equity) — this confirms
  `.44` was actually exercised against real data, not skipped.
- [ ] Confirm no order appears in the Alpaca paper-account dashboard's order
  history (there shouldn't be one — this is the final, outside-the-code
  confirmation that nothing was submitted).
- [ ] `git status` should show only the new output JSON file (if written
  inside the repo) and no changes to any existing tracked file — do not
  commit or push per Martin's standing instruction; this includes not
  committing the output JSON unless Martin separately asks for it to be
  kept in the repo.

## 7. What happens next

Report back the actual output (or the actual `FAIL-CLOSED` message, if any)
along with confirmation each safety check above passed. Do not proceed past
this single-symbol dry run, do not enable `run_stage1b_paper_cycle`, and do
not decide to reweight `technical_weight`/`short_technical_weight` without
a separate, explicit go-ahead.
