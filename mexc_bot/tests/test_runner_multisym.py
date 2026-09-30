"""
End-to-end test of fetch_historical_native.run() across 4 symbols
(BTC_USDT, ETH_USDT, SUI_USDT, DOGE_USDT) and a small ~30h window — the
"small 24-48h, 2-3+ additional symbols" pre-download check.

This runs the ACTUAL, UNMODIFIED runner (fetch_historical_native.run())
and the actual, unmodified core/mexc_native.py pagination/parsing code —
nothing about the runner or the adapter is mocked. The only thing
substituted is the network transport (core.mexc_native._get), which is
monkeypatched to replay real MEXC responses captured live via the
Windows-machine browser pane on 2026-09-17 (data/native/raw/**/multisym_test_2026-09-17*.json),
instead of making a live HTTP call — this cloud sandbox still can't reach
contract.mexc.com, and no device_bash tool was available this session to
run the unmodified script directly on the Windows machine (see
PILOT_REPORT.md §7, item 1, which flagged exactly this gap).

Funding fixtures here are real captured page_num=1/page_size=5 responses,
not page_size=1000 (matching FUNDING_REQUESTED_PAGE_SIZE would need a
1000-row capture per symbol, which the BTC-only pilot already verified in
full for BTC_USDT). Because the requested history window is short (2
days), the oldest row in each 5-row fixture is already well past the
cutoff, so fetch_funding_history_native's pagination loop correctly stops
after one page per symbol regardless of what page_size/page_num it
requested — this test is about the RUNNER's plumbing (pagination-loop
termination, dedup, parquet/raw/manifest writing), not re-verifying the
1000-cap fact itself.

Wall-clock freeze (added 2026-09-30, same root cause/fix pattern as
.355's test-suite fix in commit 7ba6f26): `run()` computes its own
`start_ms`/`end_ms` request window from a fresh, real
`datetime.now(timezone.utc)` -- there is no `now` injection point, by
design ("ACTUAL, UNMODIFIED runner" above). The fixtures are real MEXC
responses captured at a FIXED point in time (2026-09-17, ~03:00-08:00
UTC OHLCV / most-recent funding settlement 08:00 UTC) and `fake_get`
always replays them verbatim regardless of the requested window. As real
wall-clock time moves past that fixed capture window, `run()`'s
now-anchored 24h request window drifts away from the fixture data
entirely, and the runner's own range filtering (correctly) returns zero
rows for every symbol -- not a bug in the runner, a test-harness/fixture
staleness artifact, exactly like .355's case. Fixed the same way: freeze
`fetch_historical_native`'s notion of "now" (via `runner.datetime`, a
`datetime` subclass overriding only `.now()`) to shortly after the
fixtures' own last real timestamp, so the runner's UNMODIFIED windowing
logic lands back inside the fixtures' actual data -- this is an
additional substitution beyond `core.mexc_native._get` (module docstring
above was written before this fix; still accurate for the network
transport, no longer the ONLY substitution), not a code-behavior change.

Run: python3 tests/test_runner_multisym.py  (from the mexc_bot/ directory)
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import core.mexc_native as native
import fetch_historical_native as runner

TEST_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SUI/USDT:USDT", "DOGE/USDT:USDT"]
TEST_DATASET_ROOT = REPO_ROOT / "data" / "native_pilot_multisym_test"
FIXTURE_DIR = REPO_ROOT / "data" / "native" / "raw"

# Frozen "now" for the wall-clock-freeze fix (see module docstring): 1h
# after the fixtures' own latest real timestamp (2026-09-17 08:00:00 UTC,
# verified identical across all 4 symbols' OHLCV captures) -- the same
# "fixed 1h offset" pattern .355's test fix used, just anchored to the
# fixture capture time instead of real wall-clock time, since these
# fixtures are real captured data pinned to a fixed point, not synthetic.
FROZEN_NOW = datetime(2026, 9, 17, 9, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """`datetime` subclass overriding only `.now()`. A subclass (not a
    bare stand-in/MagicMock) so `fetch_historical_native.run()`'s other
    real `datetime.fromtimestamp(...)` calls keep working unmodified."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is not None else FROZEN_NOW.replace(tzinfo=None)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


KLINE_PATH_PREFIX = native.KLINE_PATH.split("{symbol}")[0]  # "/api/v1/contract/kline/"


def fake_get(path: str, params: dict) -> dict:
    """Stand-in for core.mexc_native._get: serves real captured responses
    instead of hitting the (unreachable, from here) network. Kline calls
    carry the symbol baked into `path` (fetch_kline_page does
    KLINE_PATH.format(symbol=native_symbol) before calling _get) — params
    has no "symbol" key for those, unlike the funding endpoint."""
    if path.startswith(KLINE_PATH_PREFIX):
        symbol = path[len(KLINE_PATH_PREFIX):]
        fixture = FIXTURE_DIR / "ohlcv" / symbol / "multisym_test_2026-09-17.json"
        return _load(fixture)
    elif path == native.FUNDING_PATH:
        symbol = params["symbol"]
        # BTC's multisym-window funding wasn't separately captured under
        # multisym_test naming — reuse the BTC pilot funding capture
        # (same shape: page_num=1, page_size=5, real, live, 2026-09-17).
        if symbol == "BTC_USDT":
            fixture = FIXTURE_DIR / "funding" / "BTC_USDT" / "pilot_live_2026-09-17_page1_size5.json"
        else:
            fixture = FIXTURE_DIR / "funding" / symbol / "multisym_test_2026-09-17_page1_size5.json"
        return _load(fixture)
    raise AssertionError(f"fake_get: no fixture routing for path={path} params={params}")


def main():
    if TEST_DATASET_ROOT.exists():
        shutil.rmtree(TEST_DATASET_ROOT)

    # history_days=1 (24h) rather than 2: the funding fixtures captured for
    # this test are single pages (page_size=5, ~40h of real depth). Since
    # fake_get always replays the SAME page regardless of page_num, a
    # cutoff deeper than the fixture's real depth would make
    # fetch_funding_history_native walk every page up to `totalPage` (324)
    # trying to reach data the mock can never produce — a test-harness
    # artifact, not a bug in the adapter (the BTC solo pilot already proved
    # real pagination terminates correctly against the live API). 24h
    # cutoff sits well inside the ~40h the fixtures actually cover.
    #
    # `runner.datetime` is frozen alongside the network transport (see
    # module docstring's "Wall-clock freeze" section) so `run()`'s
    # now-anchored 24h request window lands inside the fixtures' actual
    # 2026-09-17 data instead of drifting away from it as real time passes.
    with patch.object(native, "_get", side_effect=fake_get), \
         patch.object(runner, "datetime", _FrozenDatetime):
        manifest = runner.run(symbols=TEST_SYMBOLS, history_days=1, dataset_root=TEST_DATASET_ROOT)

    print("=== manifest.json (as written by the runner) ===")
    print(json.dumps(manifest, indent=2))

    manifest_path = TEST_DATASET_ROOT / "manifest.json"
    assert manifest_path.exists(), "runner did not write manifest.json"
    on_disk_manifest = json.loads(manifest_path.read_text())
    assert on_disk_manifest == manifest, "in-memory manifest and on-disk manifest.json diverge"

    print("\n=== Per-symbol verification ===")
    expected_native = {"BTC/USDT:USDT": "BTC_USDT", "ETH/USDT:USDT": "ETH_USDT",
                        "SUI/USDT:USDT": "SUI_USDT", "DOGE/USDT:USDT": "DOGE_USDT"}
    all_ok = True

    for ccxt_symbol in TEST_SYMBOLS:
        native_symbol = native.to_native_symbol(ccxt_symbol)
        assert native_symbol == expected_native[ccxt_symbol], \
            f"symbol mapping wrong: {ccxt_symbol} -> {native_symbol}"

        ohlcv_path = TEST_DATASET_ROOT / "ohlcv" / f"{native_symbol}_1h.parquet"
        funding_path = TEST_DATASET_ROOT / "funding" / f"{native_symbol}_funding.parquet"
        assert ohlcv_path.exists(), f"missing {ohlcv_path}"
        assert funding_path.exists(), f"missing {funding_path}"

        ohlcv_df = pd.read_parquet(ohlcv_path)
        funding_df = pd.read_parquet(funding_path)

        checks = {}
        checks["symbol_mapping"] = native_symbol == expected_native[ccxt_symbol]
        checks["ohlcv_row_count>0"] = len(ohlcv_df) > 0
        checks["ohlcv_chronological"] = ohlcv_df["timestamp"].is_monotonic_increasing
        gaps = ohlcv_df["timestamp"].diff().dropna()
        checks["ohlcv_1h_continuity"] = bool((gaps == pd.Timedelta(hours=1)).all()) if len(gaps) else True
        checks["ohlcv_no_duplicates"] = ohlcv_df["timestamp"].duplicated().sum() == 0
        checks["ohlcv_no_nulls"] = not ohlcv_df[["open", "high", "low", "close", "volume"]].isna().any().any()
        checks["ohlcv_tz_naive"] = ohlcv_df["timestamp"].dt.tz is None
        checks["funding_settlements>0"] = len(funding_df) > 0
        checks["funding_interval_8h"] = bool((funding_df["collect_cycle_hours"] == 8).all()) if len(funding_df) else True
        checks["funding_no_duplicates"] = funding_df["timestamp"].duplicated().sum() == 0
        checks["funding_tz_naive"] = funding_df["timestamp"].dt.tz is None
        checks["funding_ascending"] = funding_df["timestamp"].is_monotonic_increasing

        # Timestamp compatibility: reproduce backtester.py:78's comparison directly.
        try:
            ts = ohlcv_df["timestamp"].iloc[-1]
            _ = funding_df[funding_df["timestamp"] <= ts]
            checks["backtester_timestamp_compat"] = True
        except TypeError:
            checks["backtester_timestamp_compat"] = False

        # Raw dump presence (at least one raw kline + one raw funding page file
        # actually written by the runner for this symbol).
        raw_ohlcv_files = list((TEST_DATASET_ROOT / "raw" / "ohlcv" / native_symbol).glob("*.json")) \
            if (TEST_DATASET_ROOT / "raw" / "ohlcv" / native_symbol).exists() else []
        raw_funding_files = list((TEST_DATASET_ROOT / "raw" / "funding" / native_symbol).glob("*.json")) \
            if (TEST_DATASET_ROOT / "raw" / "funding" / native_symbol).exists() else []
        checks["raw_ohlcv_dumped"] = len(raw_ohlcv_files) > 0
        checks["raw_funding_dumped"] = len(raw_funding_files) > 0

        ok = all(checks.values())
        all_ok = all_ok and ok
        status = "PASS" if ok else "FAIL"
        print(f"\n[{native_symbol}] {status} — {len(ohlcv_df)} OHLCV rows "
              f"({ohlcv_df['timestamp'].min()} .. {ohlcv_df['timestamp'].max()}), "
              f"{len(funding_df)} funding rows")
        for k, v in checks.items():
            mark = "ok" if v else "**FAIL**"
            print(f"    {k}: {mark}")

    print("\n=== Directory structure check ===")
    expected_dirs = ["ohlcv", "funding", "raw/ohlcv", "raw/funding"]
    for d in expected_dirs:
        p = TEST_DATASET_ROOT / d
        print(f"  {d}: {'exists' if p.exists() else 'MISSING'}")
        all_ok = all_ok and p.exists()

    print("\n" + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    return all_ok


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
