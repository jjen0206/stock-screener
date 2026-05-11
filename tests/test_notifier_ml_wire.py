"""Guard the notifier → ml_predictor wire path against silent breakage.

If someone deletes the predict_for_strategy import / call inside notifier,
ML scoring silently stops happening — these tests fail loudly instead.
"""
import inspect
import re

import src.notifier as notifier_mod


def test_notifier_imports_predict_for_strategy():
    src = inspect.getsource(notifier_mod)
    # Match both single-line and parenthesized multi-line imports
    assert re.search(
        r"from\s+src\.ml_predictor\s+import\s*\(?[^)]*predict_for_strategy",
        src,
    ), "notifier.py must import predict_for_strategy from src.ml_predictor"


def test_select_top_picks_calls_predict_for_strategy():
    fn_src = inspect.getsource(notifier_mod._select_top_picks)
    assert "predict_for_strategy(" in fn_src, (
        "_select_top_picks must call predict_for_strategy(...) — "
        "ML scoring wire path is broken"
    )
