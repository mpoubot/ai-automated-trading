import os
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise RuntimeError("Missing Alpaca API credentials in .env")

print("=" * 60)
print("AURA → ALPACA MARKET DATA DIAGNOSTIC")
print("=" * 60)

client = StockHistoricalDataClient(
    api_key=API_KEY,
    secret_key=SECRET_KEY
)

request = StockBarsRequest(
    symbol_or_symbols=["AAPL"],
    timeframe=TimeFrame.Day,
    start="2026-08-01",
    end="2026-08-25",
    feed="iex",
    limit=20
)

print()
print("Request:")
print("Symbol: AAPL")
print("Timeframe: 1 Day")
print("Period: 2026-08-01 → 2026-08-25")
print("Feed: IEX")
print()

try:
    bars = client.get_stock_bars(request)

    print("Raw response received.")
    print()

    df = bars.df

    if df.empty:
        print("❌ NO MARKET DATA RETURNED")
        print()
        print("The API connection works, but no bars were returned.")
    else:
        print("✅ MARKET DATA RECEIVED")
        print()
        print(df)

        print()
        print(f"Number of bars: {len(df)}")

except Exception as e:
    print()
    print("❌ MARKET DATA ERROR")
    print(type(e).__name__)
    print(str(e))

print()
print("=" * 60)
print("MARKET DATA DIAGNOSTIC COMPLETE")
print("NO ORDERS WERE PLACED")
print("=" * 60)