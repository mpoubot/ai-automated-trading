@echo off
REM ============================================================================
REM run_aura_live.bat -- AURA Stage 1B production launch
REM Generated 2026-10-09, UPDATED 2026-10-10 (dual-bot A/B parallel run, per
REM Martin's explicit "PRODUCTION DIRECTIVE"), UPDATED AGAIN 2026-10-10
REM (dual-dashboard port isolation, per Martin's "PRODUCTION MANDATE --
REM DUAL-DASHBOARD PORT ISOLATION FOR BOT 2"), UPDATED AGAIN 2026-10-10
REM (third parallel bot + dashboard, per Martin's "PRODUCTION DIRECTIVE --
REM SEPARATE BOT SESSION & DEDICATED DASHBOARD FOR BOT 3"). Launches FIVE
REM concurrent windows:
REM   1. aura_v05366_live_trader_dashboard.py  -- read-only Streamlit dashboard
REM      for Bot 1, the real companion to .365 (reads .365's real output dir,
REM      makes real read-only Alpaca position/kill-switch calls). NOT .358,
REM      which is built for .357's dry-run-only output and would show stale/
REM      empty data here. CORRECTION to the port assumption in Martin's
REM      request: .366 has no --port argument of its own, and this script
REM      never passed Streamlit's own --server.port flag either -- the
REM      "standing default port" has actually always been Streamlit's own
REM      default (8501), not 5000. Left UNCHANGED/unpinned here (still
REM      whatever Streamlit's own default is) to avoid disturbing Bot 1's
REM      already-working window; see Window 1b below for the new second
REM      instance, which IS explicitly pinned to port 5001 as requested.
REM   1b. aura_v05366_live_trader_dashboard.py -- a SECOND, independent
REM      instance of the SAME dashboard script, for Bot 2 -- Streamlit's own
REM      `--server.port 5001` flag (there is no app-level port flag; Streamlit
REM      itself owns the port), pointed at Bot 2's --output-dir/--kill-switch-
REM      file/--daily-order-log-path (the same three Bot-2-specific paths
REM      Window 1c below uses) so it reads Bot 2's real state, not Bot 1's.
REM      "No shared state leaks between the two ports" is actually guaranteed
REM      by OS-level process isolation -- these are two independent Python/
REM      Streamlit processes in two independent cmd.exe windows, not two
REM      threads in one process -- not by anything py_compile can check;
REM      py_compile only confirms the file parses, see Final Verification
REM      note near the bottom.
REM   2. aura_v05365_stage1b_scheduled_live_trader.py -- "Bot 1 / Pinned Core":
REM      the original, already-live continuous unattended live-paper trading
REM      loop over the 39-symbol pinned universe, PRIMARY Alpaca paper account
REM      (via this shell's own .env-loaded ALPACA_EQUITY_PAPER_* credentials,
REM      UNCHANGED from before this update), --enable-institutional-gates
REM      (real portfolio/Greeks/macro-bucket enforcement, 0.20 stacking
REM      threshold -- as of 2026-10-10 this now ALSO catches every pinned
REM      symbol that was previously unmapped, via macro_buckets.py's new
REM      catch-all -- see that module's own docstring; Track B short-
REM      protective gates stay inert -- no real borrow-fee/short-interest
REM      vendor feed exists yet).
REM   3. aura_v05365_stage1b_scheduled_live_trader.py -- "Bot 2 / S&P 500
REM      Expansion": a SECOND, independent instance of the SAME script,
REM      scanning the 455-symbol high-confidence S&P 500 universe
REM      (aura_v05351_sp500_requests_config.json) instead of the pinned 39,
REM      on a SEPARATE Alpaca paper account (credentials set LOCALLY, in this
REM      window's own cmd session only, below -- never written to .env, never
REM      visible to Window 2). Same --enable-institutional-gates (same 0.20
REM      stacking threshold, same macro_buckets.py catch-all, same shared
REM      PROPOSED_MACRO_BUCKET_CONFIG singleton Bot 1 uses -- there is
REM      deliberately no separate "Bot 2 only" config; see macro_buckets.py's
REM      docstring for why this is intentional and global). Every output path
REM      (--strategy-id/--output-dir/--decision-journal-path/--equity-history-
REM      log-path/--daily-order-log-path/--kill-switch-file) is explicitly
REM      overridden to a Bot-2-specific value -- see inline comment on that
REM      window below for exactly why each one needed an explicit override
REM      (several of .365's own defaults are NOT strategy-id-scoped and WOULD
REM      silently collide with Bot 1's files if left at default).
REM
REM PREREQUISITES -- confirm all of these before running this script:
REM   1. feature/institutional-stage1b-wiring is merged into main, and this
REM      checkout is actually on main (git checkout main && git pull origin main).
REM   2. .venv exists at the repo root and has every dependency installed
REM      (streamlit included -- this script does not install anything).
REM   3. Window 2 (Bot 1): ALPACA_EQUITY_PAPER_API_KEY / ALPACA_EQUITY_PAPER_
REM      SECRET_KEY are set in the environment this script runs in (e.g. via
REM      your .env loading, or set them before double-clicking this file) --
REM      UNCHANGED from before, this is the PRIMARY account.
REM   4. Window 3 (Bot 2): replace BOTH "YOUR_SECONDARY_ALPACA_..._HERE" values
REM      below with the real API key/secret for your SECOND, SEPARATE Alpaca paper
REM      account before running this script. Leaving the placeholders in
REM      place will make .365 fail closed at startup (Stage3CliError on
REM      missing/empty credentials) -- it will NOT silently fall back to the
REM      primary account's credentials (confirmed: neither .356 nor .365 ever
REM      call load_dotenv(), and credential loading is a plain os.getenv()
REM      with no crypto-account fallback -- see .356's load_equity_paper_
REM      credentials()). This also means Window 2 and Window 3's credentials
REM      cannot cross-contaminate each other: each `start` below opens its own
REM      independent cmd.exe process, and `set` inside one window's command
REM      chain never affects the others or this outer script's own shell.
REM   5. You have actually reviewed and want --max-new-orders-per-cycle's
REM      default (currently uncapped at the CLI level besides its own
REM      default) and the 300s/30s/2s snapshot-age/fill-poll-timeout/fill-
REM      poll-interval values baked in below, for BOTH bots.
REM
REM This script does NOT set AURA_CORE_DIR itself -- .365 sets it internally
REM via os.environ.setdefault("AURA_CORE_DIR", str(ROOT)) at import time,
REM pointed at its own repo root. The check below is purely informational.
REM ============================================================================

setlocal

set "AURA_REPO=C:\Users\Martin\Desktop\AI agents\AI automated trading\aura-autonomous-trading-agent"
set "LOG_FILE=live_evidence_runs\production_launch_log.txt"
set "LOG_FILE_BOT2=live_evidence_runs\production_launch_log_bot2_sp500.txt"
set "LOG_FILE_BOT3=live_evidence_runs\production_launch_log_bot3_highrisk.txt"

cd /d "%AURA_REPO%"
if not exist "live_evidence_runs" mkdir "live_evidence_runs"
if not exist "stage1b_live_trader_output" mkdir "stage1b_live_trader_output"
if not exist "stage1b_live_trader_output_bot2_sp500" mkdir "stage1b_live_trader_output_bot2_sp500"

echo ============================================================
echo AURA PRODUCTION LAUNCH -- %DATE% %TIME%
echo Repo: %AURA_REPO%
if defined AURA_CORE_DIR (
    echo AURA_CORE_DIR already set in this shell: %AURA_CORE_DIR%
) else (
    echo AURA_CORE_DIR not set in this shell -- informational only:
    echo .365 sets it internally to its own repo root at import time.
)
echo ============================================================
echo.
echo Launching dashboard window 1 (.366, Streamlit port 8501 default, Bot 1 output only)...
echo Launching dashboard window 2 (.366, Streamlit port 5001, Bot 2 output only)...
echo Launching dashboard window 3 (.366, Streamlit port 5002, Bot 3 output only)...
echo Launching Bot 1 / Pinned Core window (.365, primary Alpaca account, --enable-institutional-gates)...
echo Launching Bot 2 / S&P 500 Expansion window (.365, SEPARATE Alpaca account, --enable-institutional-gates)...
echo Launching Bot 3 / High-Risk Russell 2000 window (.365, THIRD SEPARATE Alpaca account, --enable-institutional-gates)...
echo Bot 1 stdout/stderr -^> %LOG_FILE% ^(appended, not shown live in its window^)
echo Bot 2 stdout/stderr -^> %LOG_FILE_BOT2% ^(appended, not shown live in its window^)
echo Bot 3 stdout/stderr -^> %LOG_FILE_BOT3% ^(appended, not shown live in its window^)
echo Bot 1 kill switch: create "%AURA_REPO%\STOP_365_LIVE_TRADER" to stop Bot 1 before its next cycle.
echo Bot 2 kill switch: create "%AURA_REPO%\STOP_365_LIVE_TRADER_BOT2_SP500" to stop Bot 2 before its next cycle -- INDEPENDENT of Bot 1's.
echo Bot 3 kill switch: create "%AURA_REPO%\STOP_365_LIVE_TRADER_BOT3_HIGHRISK" to stop Bot 3 before its next cycle -- INDEPENDENT of Bot 1 and Bot 2.
echo.

REM --- Window 1: read-only live dashboard (.366) -- Bot 1's output dir only --
REM UNCHANGED from before -- no explicit --server.port, so this keeps using
REM whatever Streamlit's own default port is (8501) exactly as it always has.
start "AURA Dashboard - Bot1 PinnedCore (.366)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && streamlit run aura_v05366_live_trader_dashboard.py -- --output-dir stage1b_live_trader_output"

REM --- Window 1b: SECOND dashboard instance, Bot 2's output dir, port 5001 --
REM Streamlit's own --server.port flag goes BEFORE the "--" separator;
REM everything after "--" is passed through to .366's own argparse. Also
REM overrides --kill-switch-file/--daily-order-log-path to Bot 2's actual
REM paths (both default, inside .366 itself, to .365's Bot-1-shaped shared
REM defaults -- same collision class already fixed for the trader windows
REM below) so this dashboard reads Bot 2's real kill-switch/order-log state,
REM not Bot 1's.
start "AURA Dashboard - Bot2 SP500 (.366)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && streamlit run aura_v05366_live_trader_dashboard.py --server.port 5001 -- --output-dir stage1b_live_trader_output_bot2_sp500 --kill-switch-file STOP_365_LIVE_TRADER_BOT2_SP500 --daily-order-log-path regime_output\live_trader_order_log\stage1b_daily_order_log_bot2_sp500.jsonl"
REM --- Window 1c: THIRD dashboard instance, Bot 3's output dir, port 5002 -
REM Same pattern as Window 1b: Streamlit's own --server.port 5002 flag
REM before the "--" separator, Bot-3-specific --output-dir/--kill-switch-
REM file/--daily-order-log-path after it, so this dashboard reads Bot 3's
REM real state only -- completely independent of Window 1 and Window 1b.
start "AURA Dashboard - Bot3 HighRisk (.366)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && streamlit run aura_v05366_live_trader_dashboard.py --server.port 5002 -- --output-dir stage1b_live_trader_output_bot3_highrisk --kill-switch-file STOP_365_LIVE_TRADER_BOT3_HIGHRISK --daily-order-log-path regime_output\live_trader_order_log\stage1b_daily_order_log_bot3_highrisk.jsonl"


REM --- Window 2: Bot 1 / Pinned Core -- primary Alpaca account, UNCHANGED ----
REM NOTE: stdout/stderr are fully redirected to the log file below, so this
REM window will appear blank while running -- that is the "append to a
REM rolling file" behavior Task 2 asked for, not a hang. Tail the log file in
REM a separate window to watch it live, e.g.:
REM   powershell -Command "Get-Content '%AURA_REPO%\%LOG_FILE%' -Wait -Tail 50"
start "AURA Bot 1 - Pinned Core (.365)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && (echo ===== %DATE% %TIME% NEW LAUNCH ===== & python aura_v05365_stage1b_scheduled_live_trader.py --strategy-id aura_core_pinned --scan-pinned-universe --max-snapshot-age-seconds 300 --fill-poll-timeout-seconds 30 --fill-poll-interval-seconds 2 --enable-institutional-gates --i-confirm-this-runs-unattended-live-paper-trading) 1>>%LOG_FILE% 2>&1"

REM --- Window 3: Bot 2 / S&P 500 Expansion -- SEPARATE Alpaca account --------
REM Credentials below are set ONLY inside this one cmd.exe window's own
REM session (via the `set` commands in this same command chain, evaluated
REM AFTER activate.bat, BEFORE python starts) -- they never touch Window 2,
REM never touch this outer script's shell, and are never written to .env.
REM REPLACE THE TWO PLACEHOLDER VALUES BELOW before running this script.
REM
REM Every path flag is explicitly overridden because .365's own defaults are
REM NOT strategy-id-scoped and would otherwise silently collide with Bot 1:
REM   --strategy-id              distinct label, shows up in logs/journal entries
REM   --output-dir               separate from Bot 1's stage1b_live_trader_output
REM   --decision-journal-path    .361's own default is ONE shared file
REM                               (regime_output\decision_journal\stage1b_decisions.jsonl)
REM                               for ALL callers -- without this override, Bot 1
REM                               and Bot 2 would append to the SAME journal file
REM                               concurrently.
REM   --equity-history-log-path  .364's own default is likewise ONE shared file
REM                               (regime_output\equity_history_log\alpaca_equity_history.jsonl)
REM                               -- without this override, Bot 1's and Bot 2's
REM                               equity curves (two DIFFERENT Alpaca accounts)
REM                               would interleave into one history, breaking the
REM                               clean A/B comparison this whole exercise is for.
REM   --daily-order-log-path     .365's own default is likewise ONE shared file.
REM   --kill-switch-file         so Martin can stop either bot independently.
start "AURA Bot 2 - SP500 Expansion (.365)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && set ALPACA_EQUITY_PAPER_API_KEY=PKDZSXC6R2LLNMGD3GAVJ3QZHT && set ALPACA_EQUITY_PAPER_SECRET_KEY=2bBeh3thSqp2YBMka2nmCLwHhwpg1LuHe7SGK2DrHe4L && (echo ===== %DATE% %TIME% NEW LAUNCH - BOT2 SP500 ===== & python aura_v05365_stage1b_scheduled_live_trader.py --requests-config aura_v05351_sp500_requests_config.json --strategy-id SP500_EXPANSION_BOT2 --output-dir stage1b_live_trader_output_bot2_sp500 --decision-journal-path regime_output\decision_journal\stage1b_decisions_bot2_sp500.jsonl --equity-history-log-path regime_output\equity_history_log\alpaca_equity_history_bot2_sp500.jsonl --daily-order-log-path regime_output\live_trader_order_log\stage1b_daily_order_log_bot2_sp500.jsonl --kill-switch-file STOP_365_LIVE_TRADER_BOT2_SP500 --max-snapshot-age-seconds 300 --fill-poll-timeout-seconds 30 --fill-poll-interval-seconds 2 --enable-institutional-gates --i-confirm-this-runs-unattended-live-paper-trading) 1>>%LOG_FILE_BOT2% 2>&1"

REM --- Window 4: Bot 3 / High-Risk Russell 2000 -- THIRD SEPARATE Alpaca ---
REM account. Same isolation pattern as Window 3: credentials are set ONLY
REM inside this one cmd.exe window's own session, never written to .env,
REM never visible to Window 2 or Window 3. REPLACE THE TWO PLACEHOLDER
REM VALUES BELOW before running this script. Universe is the 50-ticker
REM aura_v05351_highrisk_universe.json / aura_v05351_highrisk_requests_
REM config.json working set (Russell 2000, ranked by IWM index weight as a
REM liquidity proxy -- see that file's own description field for the full
REM methodology and its stated limitation: this is NOT a true beta-screened
REM list). Same shared --enable-institutional-gates / 0.20 macro_buckets.py
REM catch-all as Bot 1 and Bot 2 -- no separate "Bot 3 only" config, same
REM reasoning as Window 3's own comment above. Every path flag is again
REM explicitly overridden to a Bot-3-specific value for the same reason as
REM Window 3: .365's own defaults are NOT strategy-id-scoped and would
REM otherwise silently collide with Bot 1 and/or Bot 2.
start "AURA Bot 3 - HighRisk Russell2000 (.365)" /D "%AURA_REPO%" cmd /k "call .venv\Scripts\activate.bat && set ALPACA_EQUITY_PAPER_API_KEY=PKF7333KG3QYW4ZGNDEICIK4EF && set ALPACA_EQUITY_PAPER_SECRET_KEY=86Nu4UJH49tvy49TEQUsiqAHdVwPMbtyaANDkuQNkJpD && (echo ===== %DATE% %TIME% NEW LAUNCH ===== & python aura_v05365_stage1b_scheduled_live_trader.py --requests-config aura_v05351_highrisk_requests_config.json --strategy-id HIGHRISK_RUSSELL2000_BOT3 --output-dir stage1b_live_trader_output_bot3_highrisk --decision-journal-path regime_output\decision_journal\stage1b_decisions_bot3_highrisk.jsonl --equity-history-log-path regime_output\equity_history_log\alpaca_equity_history_bot3_highrisk.jsonl --daily-order-log-path regime_output\live_trader_order_log\stage1b_daily_order_log_bot3_highrisk.jsonl --kill-switch-file STOP_365_LIVE_TRADER_BOT3_HIGHRISK --max-snapshot-age-seconds 300 --fill-poll-timeout-seconds 30 --fill-poll-interval-seconds 2 --enable-institutional-gates --i-confirm-this-runs-unattended-live-paper-trading) 1>>%LOG_FILE_BOT3% 2>&1"

echo.
echo All five windows launched.
echo   Dashboard 1 (Bot 1): check its window for the local Streamlit URL (default http://localhost:8501)
echo   Dashboard 2 (Bot 2): http://localhost:5001
echo   Dashboard 3 (Bot 3): http://localhost:5002
echo   Bot 1 log:        %AURA_REPO%\%LOG_FILE%
echo   Bot 1 cycle JSON: %AURA_REPO%\stage1b_live_trader_output\
echo   Bot 2 log:        %AURA_REPO%\%LOG_FILE_BOT2%
echo   Bot 2 cycle JSON: %AURA_REPO%\stage1b_live_trader_output_bot2_sp500\
echo   Bot 3 log:        %AURA_REPO%\%LOG_FILE_BOT3%
echo   Bot 3 cycle JSON: %AURA_REPO%\stage1b_live_trader_output_bot3_highrisk\
echo.
echo REMINDER: the Bot 2 trader window will fail closed at startup if you have
echo not replaced YOUR_SECONDARY_ALPACA_KEY_ID_HERE / YOUR_SECONDARY_ALPACA_SECRET_KEY_HERE
echo above with your second Alpaca paper account's real credentials.
echo REMINDER: the Bot 3 trader window will fail closed at startup if you have
echo not replaced YOUR_TERTIARY_ALPACA_KEY_ID_HERE / YOUR_TERTIARY_ALPACA_SECRET_KEY_HERE
echo above with your THIRD, separate Alpaca paper account's real credentials.
echo.
endlocal
