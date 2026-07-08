import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pandas as pd

from stockdata_hub.core import DataProvider, DataProviderManager
from stockdata_hub.normalization import CANONICAL_COLUMNS


class StubProvider(DataProvider):
    def __init__(self, name, priority, df=None, error=None, can=True):
        self.name = name
        self.priority = priority
        self._df = df
        self._error = error
        self._can = can

    def can_handle(self, symbol: str) -> bool:
        return self._can

    def fetch_data(self, symbol, days=30):
        return self._df, self._error


def _canned_df():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "open": [10.0, 10.5],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.2],
            "volume": [1000.0, 1200.0],
        }
    )


def test_add_provider_sorts_by_priority():
    mgr = DataProviderManager()
    mgr.add_provider(StubProvider("b", 5, _canned_df()))
    mgr.add_provider(StubProvider("a", 1, _canned_df()))
    mgr.add_provider(StubProvider("c", 3, _canned_df()))
    names = [p.get_name() for p in mgr.providers]
    assert names == ["a", "c", "b"]


def test_get_data_returns_first_success():
    good = StubProvider("good", 1, _canned_df())
    mgr = DataProviderManager()
    mgr.add_provider(good)
    df, err = mgr.get_data("600519", days=2)
    assert err is None
    assert list(df.columns) == CANONICAL_COLUMNS
    assert len(df) == 2
    assert mgr.get_last_used_provider() == "good"


def test_get_data_skips_failing_provider():
    failing = StubProvider("fail", 1, None, "boom")
    good = StubProvider("good", 2, _canned_df())
    mgr = DataProviderManager()
    mgr.add_provider(failing)
    mgr.add_provider(good)
    df, err = mgr.get_data("600519", days=2)
    assert df is not None
    assert mgr.get_last_used_provider() == "good"


def test_remove_provider():
    p = StubProvider("x", 1, _canned_df())
    mgr = DataProviderManager()
    mgr.add_provider(p)
    mgr.remove_provider("x")
    assert mgr.providers == []


def test_set_priority():
    a = StubProvider("a", 1)
    b = StubProvider("b", 2)
    mgr = DataProviderManager()
    mgr.add_provider(a)
    mgr.add_provider(b)
    mgr.set_provider_priority("b", 0)
    assert [p.get_name() for p in mgr.providers] == ["b", "a"]


def test_build_default_has_providers():
    mgr = DataProviderManager.build_default()
    assert len(mgr.providers) >= 5
    # 全部实现了抽象接口
    for p in mgr.providers:
        assert isinstance(p, DataProvider)
        assert p.can_handle("600519") in (True, False)


if __name__ == "__main__":
    test_add_provider_sorts_by_priority()
    test_get_data_returns_first_success()
    test_get_data_skips_failing_provider()
    test_remove_provider()
    test_set_priority()
    test_build_default_has_providers()
    print("test_core OK")
