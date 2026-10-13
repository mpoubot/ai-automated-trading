@echo off
REM ===================================================================
REM bot_credentials.bat -- per-bot Alpaca paper credentials.
REM
REM TEMPLATE -- safe to commit. Copy to bot_credentials.bat and fill
REM in the real values there. bot_credentials.bat is gitignored.
REM
REM Usage, from inside a launched window:
REM     call bot_credentials.bat bot2
REM ...which sets ALPACA_EQUITY_PAPER_API_KEY / _SECRET_KEY for THAT
REM window only. Calling with "bot1" deliberately sets nothing, so
REM Bot 1 keeps using the ambient / .env credentials exactly as before.
REM
REM Both the trader window AND its dashboard window must make this
REM call, or the dashboard reads a different account than its bot.
REM ===================================================================

if /i "%~1"=="bot1" goto :bot1
if /i "%~1"=="bot2" goto :bot2
if /i "%~1"=="bot3" goto :bot3

echo [bot_credentials] ERROR: unknown bot id "%~1" (expected bot1^|bot2^|bot3). 1>&2
exit /b 1

:bot1
REM Primary account -- intentionally no-op. Bot 1 inherits whatever
REM ALPACA_EQUITY_PAPER_* the launching shell already has (.env).
if not defined ALPACA_EQUITY_PAPER_API_KEY (
    echo [bot_credentials] ERROR: bot1 needs ALPACA_EQUITY_PAPER_API_KEY in the environment ^(.env^), but it is not set. 1>&2
    exit /b 1
)
exit /b 0

:bot2
set "ALPACA_EQUITY_PAPER_API_KEY=YOUR_BOT2_ALPACA_KEY_ID_HERE"
set "ALPACA_EQUITY_PAPER_SECRET_KEY=YOUR_BOT2_ALPACA_SECRET_KEY_HERE"
exit /b 0

:bot3
set "ALPACA_EQUITY_PAPER_API_KEY=YOUR_BOT3_ALPACA_KEY_ID_HERE"
set "ALPACA_EQUITY_PAPER_SECRET_KEY=YOUR_BOT3_ALPACA_SECRET_KEY_HERE"
exit /b 0
