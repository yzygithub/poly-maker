"""The scanner: sweep Gamma for political markets, score, persist to SQLite.

Replaces the v1 data_updater (hour-long crawl of every order book, written to
Google Sheets). A politics-filtered sweep here is seconds and one process.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from polymaker.catalog.gamma import (
    POLITICS_TAG_SLUG,
    GammaClient,
    fetch_reward_rates,
    parse_market,
)
from polymaker.catalog.scoring import score_market
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta
from polymaker.logging import get_logger

log = get_logger("catalog.scanner")


@dataclass(frozen=True, slots=True)
class ScanConfig:
    tag_slug: str = POLITICS_TAG_SLUG
    min_liquidity: float = 1000.0
    min_volume_24hr: float = 0.0
    rewards_only: bool = True  # keep only markets in the liquidity-rewards program
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    # 跳过 N 小时内就要结算的市场。落进 profile 的 `reduce_only_hours` 后引擎
    # 会转成 REDUCE_ONLY（再往里 `halt_before_hours` 则是 HALTED），既不能报价
    # 也赚不到奖励。打分函数没有时间项，不加这道过滤，快到期的市场会排第一。
    # 0 表示关闭过滤。
    min_hours_to_end: float = 0.0


async def run_scan(store: CatalogStore, cfg: ScanConfig) -> list[MarketMeta]:
    """抓取、解析、过滤、打分并落库。返回保留下来的市场。

    奖励费率来自 CLOB，在 `rewards_only=True` 时是必需的（没有它，每个候选
    市场都像“没有奖励”，结果一个都留不下），所以那种情况下失败仍然致命。
    `rewards_only=False` 时降级为空费率继续扫 —— CLOB 抖一下不该让整个发现
    流程挂掉。
    """
    try:
        reward_rates = await fetch_reward_rates(cfg.clob_host)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if cfg.rewards_only:
            raise
        log.warning("reward_rates_unavailable", err=str(exc),
                    note="continuing without rewards (rewards_only=False)")
        reward_rates = {}
    log.info("reward_rates_loaded", n=len(reward_rates))

    kept: list[MarketMeta] = []
    skipped_expiring = 0
    async with GammaClient(cfg.gamma_host) as gamma:
        tag_id = store.cached_tag(cfg.tag_slug) or await gamma.resolve_tag_id(cfg.tag_slug)
        if tag_id:
            store.cache_tag(cfg.tag_slug, tag_id)

        seen = 0
        async for raw in gamma.iter_markets(
            tag_id=tag_id,
            min_liquidity=cfg.min_liquidity,
            min_volume_24hr=cfg.min_volume_24hr,
        ):
            seen += 1
            meta = parse_market(raw, reward_rates)
            if meta is None:
                continue
            if cfg.rewards_only and meta.rewards_daily_rate <= 0:
                continue
            hours = meta.hours_to_end  # None（无到期日）按原样保留
            if cfg.min_hours_to_end > 0 and hours is not None and hours < cfg.min_hours_to_end:
                skipped_expiring += 1
                continue
            kept.append(meta)

    for m in kept:
        store.upsert_market(m, score_market(m))
    log.info("scan_complete", seen=seen, kept=len(kept), tag=cfg.tag_slug,
             skipped_expiring=skipped_expiring)
    return kept
