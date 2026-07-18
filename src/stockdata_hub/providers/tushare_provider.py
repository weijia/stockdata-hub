#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare Pro 日线行情 Provider。

数据来自 Tushare Pro（https://tushare.pro），独立于东方财富，可作为被墙东财的
高质量替代源。提供 A股 / ETF / 指数 / 基金的日线行情。

依赖（可选）：``tushare``。未安装、缺少 token 时 ``can_handle`` 返回 ``False``，
由管理器自动跳过，不影响其它源。

配置：
- 需要 Tushare Pro token，通过环境变量 ``TUSHARE_TOKEN`` 提供（推荐），
  或在进程中已 ``ts.set_token(...)``。

成交量单位：tushare ``daily`` 返回的 ``vol`` 单位即「手」，符合统一契约，无需换算。
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

import pandas as pd

from ..code_utils import StockCodeNormalizer
from ..core import DataProvider

logger = logging.getLogger(__name__)

try:
    import tushare as ts

    TUSHARE_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖可选
    TUSHARE_AVAILABLE = False
    ts = None  # type: ignore[assignment]
    logger.debug("tushare 未安装，TushareProvider 不可用。安装: pip install tushare")


def _to_ts_code(symbol: str) -> Optional[str]:
    """把 6 位纯数字代码转为 tushare 的 ``000001.SZ`` 格式。

    规则（与 A股/基金代码段对应）：6 开头→SH；0/3 开头→SZ；
    5/9 开头（上交所基金/可转债等）→SH；1 开头（深交基金）→SZ。
    """
    if not (symbol.isdigit() and len(symbol) == 6):
        return None
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("5", "9")):
        return f"{symbol}.SH"
    # 0 / 1 / 3 开头均归深交所
    return f"{symbol}.SZ"


class TushareProvider(DataProvider):
    """Tushare Pro 日线行情 Provider（A股 / ETF / 指数 / 基金）。"""

    def __init__(self, token: Optional[str] = None) -> None:
        self.name = "TusharePro"
        # 优先级：放在新浪(4)之后、腾讯(5)/东财(8)附近。
        # 质量高但免费档限频严格（50 次/分），不宜作为高并发首选，作高质量补充/兜底。
        self.priority = 5
        self._pro = None
        if not TUSHARE_AVAILABLE:
            return
        token = token or os.environ.get("TUSHARE_TOKEN")
        if not token:
            logger.debug("未设置 TUSHARE_TOKEN，TushareProvider 不可用。")
            return
        try:
            ts.set_token(token)
            self._pro = ts.pro_api()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Tushare token 初始化失败: {e}")
            self._pro = None

    def can_handle(self, symbol: str) -> bool:
        if self._pro is None:
            return False
        mt = StockCodeNormalizer.get_market_type(symbol)
        return mt in ("A", "ETF")

    def fetch_data(
        self, symbol: str, days: int = 30
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if self._pro is None:
            return None, "tushare 不可用（未安装或缺少 token）"
        ts_code = _to_ts_code(symbol)
        if ts_code is None:
            return None, f"无法识别的 tushare 代码: {symbol}"
        try:
            end_dt = pd.Timestamp.now()
            # 请求的起始日：days 之外多留 400 天缓冲，避免边界丢数据
            start_req = end_dt - pd.Timedelta(days=days + 400)

            # tushare daily 单次返回有上限（约 5000 行），全量回补一只股票可能逼近。
            # 按 ~4000 交易日（≈15 年）分段时间滚动拉取并合并，保证长历史不截断。
            fragments: List[pd.DataFrame] = []
            cursor_end = end_dt
            for _ in range(8):  # 最多 8 段，足以覆盖数十年
                seg_start = cursor_end - pd.Timedelta(days=4000)
                if seg_start < start_req:
                    seg_start = start_req
                df = self._pro.daily(
                    ts_code=ts_code,
                    start_date=seg_start.strftime("%Y%m%d"),
                    end_date=cursor_end.strftime("%Y%m%d"),
                )
                if df is not None and not df.empty:
                    fragments.append(df)
                    earliest = pd.to_datetime(df["trade_date"].min(), format="%Y%m%d")
                    if earliest <= start_req:
                        break
                    cursor_end = earliest - pd.Timedelta(days=1)
                else:
                    break

            if not fragments:
                return None, "tushare 返回空数据"

            df = pd.concat(fragments, ignore_index=True)
            # daily 列：ts_code, trade_date, open, high, low, close, pre_close,
            #           change, pct_chg, vol, amount
            df = df.rename(columns={"trade_date": "date", "vol": "volume"})
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            # 仅保留统一契约列（date/open/high/low/close/volume）
            keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
            df = df[keep].copy()
            logger.info(f"tushare 获取成功: {symbol} {len(df)} 条")
            return df, None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"tushare 获取失败: {symbol} - {e}")
            return None, f"tushare 获取失败: {str(e)}"

    def get_provider_info(self) -> dict:  # type: ignore[override]
        info = super().get_provider_info()
        info.update(
            {
                "available": self._pro is not None,
                "token_set": bool(os.environ.get("TUSHARE_TOKEN")),
            }
        )
        return info
