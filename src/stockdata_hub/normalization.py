#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一契约的规范化（normalization）。

所有 Provider 最终都应返回满足「统一契约」的 DataFrame：

    规范列: date(datetime64), open, high, low, close, volume(均为 float)
    可选列: amount, ma5, ma10, ma20
    volume 单位: 「手」(lot)，A股/ETF 1手 = 100股

:func:`normalize_ohlcv` 负责把各源「五花八门」的原始 DataFrame 收敛到该契约：
1. 列名别名归并（``time/datetime/日期 -> date``，``开盘/收盘/最高/最低/成交量 -> ...`` 等）
2. 数值化
3. ``date`` 解析为 datetime、排序、取最近 ``days`` 条
4. 可选保留规范化后的 ``ma5/ma10/ma20``
5. 丢弃 ``source`` 等多余列

注意：**成交量单位由各个 Provider 自行保证为「手」**。返回「股」的源
（如 openstockdata）应在 ``fetch_data`` 内先 ``÷ VOLUME_SHARE_TO_LOT``。
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# 统一契约的规范列
CANONICAL_COLUMNS = ["date", "open", "high", "low", "close", "volume"]

# 统一分钟契约的规范列（用 datetime 含时分秒，替代日线的 date）
CANONICAL_INTRADAY_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]

# 分钟各语义的列名别名（覆盖各源原始列名 + 中文/英文变体）。
_COLUMN_ALIASES_INTRADAY = {
    "datetime": ["datetime", "time", "date", "日期", "交易时间", "交易日期"],
    "open": ["open", "开盘"],
    "high": ["high", "最高"],
    "low": ["low", "最低"],
    "close": ["close", "收盘"],
    "volume": ["volume", "vol", "成交量"],
    "amount": ["amount", "成交额", "成交金额"],
}

# 成交量单位换算常量：股 -> 手（A股/ETF 1手 = 100股）。
# 返回「股」的源在 fetch_data 内使用本常量做 ÷100。
VOLUME_SHARE_TO_LOT = 100

# 各语义的列名别名（覆盖各源的原始列名 + 中文/英文变体）。
_COLUMN_ALIASES = {
    "date": ["date", "time", "datetime", "日期", "交易日期"],
    "open": ["open", "开盘"],
    "high": ["high", "最高"],
    "low": ["low", "最低"],
    "close": ["close", "收盘"],
    "volume": ["volume", "vol", "成交量"],
}

# MA 列规范化映射。
_MA_ALIASES = {
    "ma5avgprice": "ma5",
    "ma10avgprice": "ma10",
    "ma20avgprice": "ma20",
    "MA5": "ma5",
    "MA10": "ma10",
    "MA20": "ma20",
}


def normalize_ohlcv(
    df: pd.DataFrame, days: int = 30
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """
    将任意源的 DataFrame 规范化为统一 OHLCV 契约。

    Returns:
        ``(规范后 DataFrame, 错误信息)``。失败时为 ``(None, reason)``。
    """
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return None, "空数据"

    df = df.copy()

    # 1+2. 按别名重命名（已存在的规范列不覆盖）
    rename_map: dict = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            if alias in df.columns:
                rename_map[alias] = canonical
                break
    if rename_map:
        df = df.rename(columns=rename_map)

    # 校验必要列
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        return None, f"缺少必要列: {missing}（现有列: {list(df.columns)}）"

    # 3. 数值化
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 4. 日期化 + 清洗 + 排序 + 截取
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=CANONICAL_COLUMNS)
    if df.empty:
        return None, "清洗后无有效行"
    df = df.sort_values("date").tail(days).reset_index(drop=True)

    # 5. 可选保留规范化后的 MA 列
    keep = list(CANONICAL_COLUMNS)
    for src, dst in _MA_ALIASES.items():
        if src in df.columns:
            df[dst] = pd.to_numeric(df[src], errors="coerce")
            if dst not in keep:
                keep.append(dst)

    # 6. 仅保留规范列（+ 可选 MA），丢弃 source 等多余列
    df = df[keep].copy()
    return df, None


def to_lot(volume_series: "pd.Series") -> "pd.Series":
    """把「股」为单位的成交量换算为「手」。供返回股数的 Provider 使用。"""
    return volume_series / VOLUME_SHARE_TO_LOT


def normalize_intraday(
    df: pd.DataFrame,
    period: str = "1m",
    days: int = 1,
    count: Optional[int] = None,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """
    将任意源的分钟 DataFrame 规范化为统一分钟契约。

    与 :func:`normalize_ohlcv` 平行，仅把 ``date`` 升格为 ``datetime``（含时分秒）。
    不改动 :func:`normalize_ohlcv`，确保日线回归零风险（需求 FR-5 / 设计 §4）。

    Returns:
        ``(规范后 DataFrame, 错误信息)``。失败时为 ``(None, reason)``。
    """
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return None, "空数据"

    df = df.copy()

    # 1. 按别名重命名（已存在的规范列不覆盖）
    rename_map: dict = {}
    for canonical, aliases in _COLUMN_ALIASES_INTRADAY.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            if alias in df.columns:
                rename_map[alias] = canonical
                break
    if rename_map:
        df = df.rename(columns=rename_map)

    # 2. 校验必要列
    missing = [c for c in CANONICAL_INTRADAY_COLUMNS if c not in df.columns]
    if missing:
        return None, f"缺少必要列: {missing}（现有列: {list(df.columns)}）"

    # 3. 数值化
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # 4. datetime 解析为 datetime64（保留时分秒）
    #    支持三类常见形态：
    #    - 已经是 datetime64（多数源经 Provider 预处理后传入）→ 直接保留
    #    - mootdx 原始整数/字符串 YYYYMMDDHHMM（如 202607171501）
    #    - 标准日期时间字符串（如 "2024-01-01 09:30:00"，东财/新浪风格）
    #    注意：mootdx 的整数若交给 pandas 自动解析，会被当成「纳秒时间戳」误判成
    #    1970 年且非 NaT，故必须先按 %Y%m%d%H%M 显式解析，失败再回退自动解析。
    s = df["datetime"]
    if pd.api.types.is_datetime64_any_dtype(s):
        dt = s
    else:
        dt = pd.to_datetime(s.astype(str), format="%Y%m%d%H%M", errors="coerce")
        if dt.isna().all():
            dt = pd.to_datetime(s, errors="coerce")
    df["datetime"] = dt
    df = df.dropna(subset=CANONICAL_INTRADAY_COLUMNS)
    if df.empty:
        return None, "清洗后无有效行"
    df = df.sort_values("datetime").reset_index(drop=True)

    # 5. 窗口过滤：保留 datetime >= now - days 个日历日
    #    （分钟数据需含当日盘中实时部分，用日历日而非交易日，设计 §4）
    if days and days > 0:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        filtered = df[df["datetime"] >= cutoff]
        # 软回退：若过滤后为空（如周末/节假日最近交易日落在窗口外），
        # 保留全部，避免误删有效的历史分钟数据。
        df = filtered if not filtered.empty else df
    if df.empty:
        return None, "窗口过滤后无数据"

    # 6. 可选 count 截断：取最后 count 根
    if count:
        df = df.tail(count).reset_index(drop=True)

    # 7. 仅保留规范列（+ 可选 amount），丢弃多余列
    keep = list(CANONICAL_INTRADAY_COLUMNS)
    if "amount" in df.columns:
        keep.append("amount")
    df = df[keep].copy()
    return df, None
