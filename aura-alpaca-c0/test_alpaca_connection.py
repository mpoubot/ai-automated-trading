import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

# Load API credentials from .env
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise RuntimeError(
        "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env"
    )

print("=" * 60)
print("AURA → ALPACA CONNECTION TEST")
print("=" * 60)

# IMPORTANT: paper=True means this connects to the paper account
client = TradingClient(
    api_key=API_KEY,
    secret_key=SECRET_KEY,
    paper=True
)

# Read account information
account = client.get_account()

print(f"Account ID:        {account.id}")
print(f"Status:            {account.status}")
print(f"Currency:          {account.currency}")
print(f"Equity:            ${account.equity}")
print(f"Cash:              ${account.cash}")
print(f"Buying Power:      ${account.buying_power}")
print(f"Trading Blocked:   {account.trading_blocked}")
print(f"Account Blocked:   {account.account_blocked}")
print(f"Pattern Day Trader:{account.pattern_day_trader}")
print()
print("READ-ONLY TEST COMPLETE")
print("NO ORDER WAS PLACED")
print("=" * 60)