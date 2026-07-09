#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯批量实时行情 Provider（高速，零额外依赖，仅用标准库 urllib）。

核心优势：
- 一次 HTTP 请求可批量获取最多 800 只股票的实时快照（价格 / PE / PB / 市值 ...）。
- 不封 IP，响应 < 1 秒/100 只。

注意：腾讯接口返回的是**当日实时快照**，不是历史 K线。当 ``days > 1`` 时本
Provider 主动返回 ``(None, reason)`` 让管理器跳过，交由 mootdx / akshare 获取历史 K线。
因此本源适合「快速看当前价」，不适合「拉历史」。

成交量单位：腾讯接口返回的 ``volume`` 为「手」，符合统一契约。
"""
from __future__ import annotations

import logging
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from ..core import DataProvider

logger = logging.getLogger(__name__)


def _to_float(value) -> float:
    """容错转 float：空串、'--'、None 等非法值统一返回 0.0。"""
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def tencent_quote_batch(codes: List[str]) -> Dict[str, Dict]:
    """
    批量拉取腾讯财经实时行情（一次请求最多 800 只）。

    Args:
        codes: 股票代码列表，如 ``["000001", "600036", "510050", "01810", "00700"]``。

    Returns:
        ``{code: {name, price, open, high, low, close, volume, amount, ...}}``
    """
    if not codes:
        return {}

    prefixed: List[str] = []
    for c in codes:
        c = c.strip()
        if c.lower().startswith(("sh", "sz", "bj", "hk")):
            prefixed.append(c.lower())
        elif len(c) <= 5 and c.isdigit():
            prefixed.append(f"hk{c.zfill(5)}")
        elif c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    all_results: Dict[str, Dict] = {}
    batch_size = 800
    for i in range(0, len(prefixed), batch_size):
        batch = prefixed[i : i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            req.add_header("Referer", "https://gu.qq.com/")
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read().decode("gbk", errors="replace")
        except Exception as e:  # noqa: BLE001
            logger.error(f"腾讯批量接口请求失败 (批次 {i // batch_size + 1}): {e}")
            continue

        for line in data.strip().split(";"):
            if "=" not in line or '"' not in line:
                continue
            try:
                key = line.split("=")[0].split("_")[-1]
                vals = line.split('"')[1].split("~")
                if len(vals) < 10:
                    continue
                code = key[2:]
                all_results[code] = {
                    "name": vals[1],
                    "price": _to_float(vals[3]),
                    "last_close": _to_float(vals[4]),
                    "open": _to_float(vals[5]),
                    "change_amt": _to_float(vals[31]),
                    "change_pct": _to_float(vals[32]),
                    "high": _to_float(vals[33]),
                    "low": _to_float(vals[34]),
                    "volume": _to_float(vals[36]),
                    "amount": _to_float(vals[37]),
                    "turnover_pct": _to_float(vals[38]),
                    "pe_ttm": _to_float(vals[39]),
                    "mcap_yi": _to_float(vals[44]),
                    "pb": _to_float(vals[46]),
                }
            except (ValueError, IndexError) as e:  # noqa: BLE001
                logger.debug(f"解析腾讯数据行失败: {e}")
                continue

    logger.info(f"腾讯批量接口成功获取 {len(all_results)}/{len(codes)} 只")
    return all_results


class FastTencentProvider(DataProvider):
    """腾讯批量实时行情 Provider（高速当日快照）。"""

    def __init__(self) -> None:
        self.name = "腾讯批量实时"
        self.priority = 0  # 最高优先级：速度最快
        self._can_handle_cache: set = set()

    def can_handle(self, symbol: str) -> bool:
        if symbol in self._can_handle_cache:
            return True
        if symbol.isdigit() and len(symbol) == 6:
            self._can_handle_cache.add(symbol)
            return True
        if symbol.isdigit() and len(symbol) <= 5:
            self._can_handle_cache.add(symbol)
            return True
        if len(symbol) >= 6 and symbol.lower().startswith(("sh", "sz", "bj", "hk")):
            self._can_handle_cache.add(symbol)
            return True
        return False

    def _is_hk(self, symbol: str) -> bool:
        """判断代码是否为港股（5 位数字，或 hk 前缀）。"""
        if symbol.lower().startswith("hk"):
            return True
        return symbol.isdigit() and len(symbol) <= 5

    def can_handle_request(self, symbol: str, days: int = 1) -> bool:
        # 腾讯接口只有当日快照，无法提供历史 K线。
        # 普通市场 days>1 时跳过，交由 mootdx/akshare 取历史；
        # 港股(5 位/hk 前缀)无其它历史源兜底，腾讯当日快照即可满足实时价需求，
        # 因此 days>1 也放行。
        if days <= 1:
            return self.can_handle(symbol)
        return self._is_hk(symbol) and self.can_handle(symbol)

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        # 腾讯接口只有当日快照，无法提供历史 K线；
        # 但港股无其它源兜底，days>1 时也用当日快照返回（仅 1 行）。
        if days > 1 and not self._is_hk(symbol):
            return None, f"腾讯批量接口仅支持当日数据(days=1)，需要{days}天历史，跳过"

        result = tencent_quote_batch([symbol])
        if symbol not in result:
            return None, f"腾讯接口未返回 {symbol} 的数据"

        q = result[symbol]
        today = pd.Timestamp.now().normalize()
        df = pd.DataFrame(
            [
                {
                    "date": today,
                    "open": q["open"],
                    "high": q["high"],
                    "low": q["low"],
                    "close": q["price"],
                    "volume": q["volume"],
                    "amount": q["amount"],
                    "change_pct": q["change_pct"],
                    "pe_ttm": q.get("pe_ttm"),
                    "pb": q.get("pb"),
                    "mcap": q.get("mcap_yi"),
                }
            ]
        )
        logger.info(f"腾讯实时数据获取成功: {symbol} @ {q['price']}")
        return df, None

    def fetch_batch(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        """批量获取多只股票当日快照：``{symbol: DataFrame}``。"""
        quotes = tencent_quote_batch(symbols)
        result: Dict[str, pd.DataFrame] = {}
        today = pd.Timestamp.now().normalize()
        for symbol in symbols:
            if symbol not in quotes:
                continue
            q = quotes[symbol]
            df = pd.DataFrame(
                [
                    {
                        "date": today,
                        "open": q["open"],
                        "high": q["high"],
                        "low": q["low"],
                        "close": q["price"],
                        "volume": q["volume"],
                        "amount": q["amount"],
                        "change_pct": q["change_pct"],
                        "pe_ttm": q.get("pe_ttm"),
                        "pb": q.get("pb"),
                        "mcap": q.get("mcap_yi"),
                    }
                ]
            )
            result[symbol] = df
        logger.info(f"批量获取完成: {len(result)}/{len(symbols)} 只成功")
        return result
