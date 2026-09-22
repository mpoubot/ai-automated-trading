"""
tests/test_strategy_interface.py

The Strategy ABC: cannot be instantiated directly, a concrete
implementation must provide required_warmup_bars()/evaluate(), and the
is_research_fixture flag is structural (used by label() and checkable by
any downstream consumer) -- not just a naming convention.
"""
import pytest

from core.instrument import Instrument
from core.strategy_base import PIPELINE_FIXTURE_LABEL, Strategy
from strategies.pipeline_fixture import PipelineValidationFixture
from tests._fixtures import make_synthetic_ohlcv


def test_strategy_is_abstract():
    with pytest.raises(TypeError):
        Strategy()  # abstract methods not implemented


def test_pipeline_fixture_declares_itself_a_fixture():
    strat = PipelineValidationFixture()
    assert strat.is_research_fixture is True
    assert strat.label() == PIPELINE_FIXTURE_LABEL


def test_a_hypothetical_real_strategy_would_not_carry_the_fixture_label():
    class _FakeRealStrategy(Strategy):
        strategy_id = "fake_real_v1"
        version = "0.1.0"
        is_research_fixture = False

        def required_warmup_bars(self) -> int:
            return 20

        def evaluate(self, window, instrument):
            return None

    strat = _FakeRealStrategy()
    assert strat.is_research_fixture is False
    assert strat.label() == "fake_real_v1 v0.1.0"
    assert PIPELINE_FIXTURE_LABEL not in strat.label()


def test_required_warmup_bars_is_a_positive_int():
    strat = PipelineValidationFixture()
    assert isinstance(strat.required_warmup_bars(), int)
    assert strat.required_warmup_bars() > 0


def test_evaluate_signature_accepts_window_and_instrument():
    strat = PipelineValidationFixture(interval_bars=10)
    ohlcv = make_synthetic_ohlcv(num_bars=30)
    ohlcv["atr"] = 1.0  # bypass ATR warmup for this interface-shape test
    inst = Instrument.mexc_swap("BTC")
    result = strat.evaluate(ohlcv.iloc[:20], inst)
    assert result is None or hasattr(result, "direction")
