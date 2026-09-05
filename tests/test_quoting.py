"""Unit tests for pure quote construction — the strategy's decision core."""

from __future__ import annotations

import pytest

from polymaker.domain import Position, Regime, Side
from polymaker.strategy.quoting import (
    QuoteInputs,
    compute_fair_value,
    construct_quotes,
    round_to_tick,
)
from tests.conftest import view


def _inputs(meta, profile, **over):
    base = dict(
        meta=meta,
        regime=Regime.QUIET,
        fv=0.50,
        vol_short=0.0,
        toxicity=0.0,
        yes_view=view(0.49, 0.51),
        no_view=view(0.49, 0.51),
        pos_yes=Position("yes-token"),
        pos_no=Position("no-token"),
        profile=profile,
        now=1000.0,
    )
    base.update(over)
    return QuoteInputs(**base)


# ── round_to_tick ──────────────────────────────────────────────────────────


def test_round_to_tick_down_and_up():
    assert round_to_tick(0.5049, 0.01, 2, up=False) == 0.50
    assert round_to_tick(0.5051, 0.01, 2, up=True) == 0.51
    # clamps inside (0,1)
    assert round_to_tick(0.0, 0.01, 2, up=False) == 0.01
    assert round_to_tick(1.0, 0.01, 2, up=True) == 0.99


def test_compute_fair_value_flow_nudge():
    # positive flow nudges FV up, negative down, no flow = microprice
    assert compute_fair_value(0.50, 0.0, 0.01) == pytest.approx(0.50)
    assert compute_fair_value(0.50, 1.0, 0.01, weight=0.5) == pytest.approx(0.505)
    assert compute_fair_value(0.50, -1.0, 0.01, weight=0.5) == pytest.approx(0.495)


# ── two-sided quoting ────────────────────────────────────────────────────────


def test_quiet_market_quotes_both_sides_as_bids(meta, profile):
    tq = construct_quotes(_inputs(meta, profile))
    assert tq.regime == Regime.QUIET
    yes = [q for q in tq.quotes if q.token_id == "yes-token"]
    no = [q for q in tq.quotes if q.token_id == "no-token"]
    assert yes and no
    # both entry quotes are BUYs (USDC-collateralized two-sided quote)
    assert all(q.side == Side.BUY for q in yes)
    assert all(q.side == Side.BUY for q in no)


def test_pair_prices_sum_below_one(meta, profile):
    """BUY YES @ p and BUY NO @ q must satisfy p + q < 1 (merge edge)."""
    tq = construct_quotes(_inputs(meta, profile))
    top_yes = max(q.price for q in tq.quotes if q.token_id == "yes-token")
    top_no = max(q.price for q in tq.quotes if q.token_id == "no-token")
    assert top_yes + top_no < 1.0


def test_never_bids_through_fair_value(meta, profile):
    """No BUY should ever sit at or above FV - min_edge (YES) / (1-FV)-min_edge (NO)."""
    tq = construct_quotes(_inputs(meta, profile, fv=0.50))
    edge = profile.min_edge_ticks * meta.tick_size
    for q in tq.quotes:
        if q.side == Side.BUY and q.token_id == "yes-token":
            assert q.price <= 0.50 - edge + 1e-9
        if q.side == Side.BUY and q.token_id == "no-token":
            assert q.price <= 0.50 - edge + 1e-9  # NO fv is also 0.50 here


def test_layers_split_size(meta, profile):
    tq = construct_quotes(_inputs(meta, profile))
    yes = sorted((q for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY),
                 key=lambda q: -q.price)
    assert len(yes) == profile.layers
    # deeper layer is at a lower price
    assert yes[0].price > yes[1].price


# ── inventory skew ──────────────────────────────────────────────────────────


def test_long_yes_inventory_skews_quotes_down(meta, profile):
    """Holding YES should lower the YES bid and raise the NO bid vs flat."""
    flat = construct_quotes(_inputs(meta, profile, vol_short=0.02))
    longy = construct_quotes(
        _inputs(meta, profile, vol_short=0.02, pos_yes=Position("yes-token", 300, 0.5))
    )

    def top(tq, tok):
        ps = [q.price for q in tq.quotes if q.token_id == tok and q.side == Side.BUY]
        return max(ps) if ps else None

    # YES bid should not be higher when long YES; NO bid should not be lower
    assert top(longy, "yes-token") <= top(flat, "yes-token")
    assert top(longy, "no-token") >= top(flat, "no-token")


def test_reduce_only_emits_only_exits(meta, profile):
    tq = construct_quotes(
        _inputs(
            meta, profile, regime=Regime.REDUCE_ONLY,
            pos_yes=Position("yes-token", 100, 0.5),
        )
    )
    assert all(q.side == Side.SELL for q in tq.quotes)
    assert any(q.token_id == "yes-token" for q in tq.quotes)


def test_event_and_halted_pull_all_quotes(meta, profile):
    for regime in (Regime.EVENT, Regime.HALTED):
        tq = construct_quotes(
            _inputs(meta, profile, regime=regime, pos_yes=Position("yes-token", 100, 0.5))
        )
        assert tq.is_empty


# ── exits ────────────────────────────────────────────────────────────────────


def test_exit_sell_priced_above_fv_when_not_urgent(meta, profile):
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 100, 0.4), yes_exit_urgency=0.0)
    )
    sells = [q for q in tq.quotes if q.side == Side.SELL and q.token_id == "yes-token"]
    assert sells
    assert sells[0].price >= 0.50  # at/above FV, a passive maker exit


def test_exit_never_below_best_bid(meta, profile):
    tq = construct_quotes(
        _inputs(
            meta, profile,
            pos_yes=Position("yes-token", 100, 0.4),
            yes_view=view(0.49, 0.51),
            yes_exit_urgency=1.0,  # maximally urgent
        )
    )
    sells = [q for q in tq.quotes if q.side == Side.SELL and q.token_id == "yes-token"]
    assert sells
    assert sells[0].price >= 0.49  # still a maker order, never crosses down


def test_no_exit_when_position_is_dust(meta, profile):
    tq = construct_quotes(
        _inputs(meta, profile, pos_yes=Position("yes-token", 1.0, 0.4))  # below min_order_size
    )
    assert not [q for q in tq.quotes if q.side == Side.SELL]


# ── spread widening ──────────────────────────────────────────────────────────


def test_toxicity_widens_spread(meta, profile):
    """Higher toxicity should push the YES bid lower (wider spread)."""
    calm = construct_quotes(_inputs(meta, profile, regime=Regime.TRENDING, toxicity=0.0))
    toxic = construct_quotes(_inputs(meta, profile, regime=Regime.TRENDING, toxicity=0.02))

    def top_yes(tq):
        ps = [q.price for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY]
        return max(ps) if ps else None

    assert top_yes(toxic) < top_yes(calm)


def test_quiet_regime_clamps_spread_to_reward_band(meta, profile):
    """In QUIET, even with high vol the bid stays within the reward band of FV."""
    tq = construct_quotes(_inputs(meta, profile, regime=Regime.QUIET, vol_short=0.5))
    band = meta.rewards_max_spread / 100.0  # 0.03
    top_yes = max(q.price for q in tq.quotes if q.token_id == "yes-token" and q.side == Side.BUY)
    # bid should be within (band + a tick of rounding) of FV
    assert top_yes >= 0.50 - band - meta.tick_size


# ── 两边入场单等额股数 ──────────────────────────────────────────────────────


def _buy_sizes(tq, token_id):
    return [q.size for q in tq.quotes if q.token_id == token_id and q.side == Side.BUY]


def test_entries_share_the_same_size(meta, profile):
    """两边价格不对称时（等额美元会给出不同股数），最终必须统一成同一个股数。

    奖励分 Q_min = min(Q_one, Q_two)、合并也只合 min(yes, no)，股数不等时多出来的
    那部分是纯浪费，还会留下方向残差。
    """
    tq = construct_quotes(_inputs(
        meta, profile, fv=0.30,
        yes_view=view(0.29, 0.31), no_view=view(0.67, 0.69),
    ))
    yes, no = _buy_sizes(tq, "yes-token"), _buy_sizes(tq, "no-token")
    assert yes and no
    assert set(yes) == set(no), (yes, no)


def test_equal_share_entries_can_be_disabled(meta, profile):
    """关掉开关则回到"各算各的"——便宜那边股数更多（这正是残差的来源）。"""
    kwargs = dict(fv=0.30, yes_view=view(0.29, 0.31), no_view=view(0.67, 0.69))
    off = construct_quotes(_inputs(meta, profile.with_overrides(
        {"equal_share_entries": False}), **kwargs))
    yes, no = _buy_sizes(off, "yes-token"), _buy_sizes(off, "no-token")
    assert sum(yes) > sum(no)  # YES 便宜 -> 股数更多 -> 合并后会剩 YES


def test_equal_share_takes_the_smaller_side(meta, profile):
    """取较小者，所以总名义只会 <= 各算各的，不会放大风险。"""
    kwargs = dict(fv=0.30, yes_view=view(0.29, 0.31), no_view=view(0.67, 0.69))
    both = construct_quotes(_inputs(meta, profile, **kwargs))
    off = construct_quotes(_inputs(meta, profile.with_overrides(
        {"equal_share_entries": False}), **kwargs))
    assert sum(_buy_sizes(both, "yes-token")) <= sum(_buy_sizes(off, "yes-token"))
    assert sum(_buy_sizes(both, "no-token")) == sum(_buy_sizes(off, "no-token"))


# ── 贴盘口（join_touch_ticks）──────────────────────────────────────────────


def _top_bid(tq, token_id):
    ps = [q.price for q in tq.quotes if q.token_id == token_id and q.side == Side.BUY]
    return max(ps) if ps else None


def _one_tick_market(**over):
    """价差只有 1 个 tick 的市场（Fed 那种）：YES 0.47/0.48，NO 0.52/0.53。"""
    base = dict(fv=0.475, yes_view=view(0.47, 0.48), no_view=view(0.52, 0.53))
    base.update(over)
    return base


def test_one_tick_market_defaults_to_second_level(meta, profile):
    """默认（join_touch_ticks=0）：delta 有 1 tick 硬下限 -> 够不到买一，只能挂买二。"""
    p = profile.with_overrides({"delta_min_ticks": 1, "min_edge_ticks": 0})
    tq = construct_quotes(_inputs(meta, p, **_one_tick_market()))
    assert _top_bid(tq, "yes-token") == 0.46   # best_bid 是 0.47 -> 买二
    assert _top_bid(tq, "no-token") == 0.51    # NO best_bid 是 0.52 -> 买二


def test_join_touch_reaches_best_bid(meta, profile):
    """开启贴盘口 + min_edge_ticks=0 -> 挂到买一。

    奖励分 S = ((v-s)/v)^2 是二次衰减的，1c 价差市场上买二(s=1.5c) 只有
    买一(s=0.5c) 的 ~56%（v=4.5c）。
    """
    p = profile.with_overrides(
        {"delta_min_ticks": 1, "min_edge_ticks": 0, "join_touch_ticks": 1})
    tq = construct_quotes(_inputs(meta, p, **_one_tick_market()))
    assert _top_bid(tq, "yes-token") == 0.47   # == best_bid
    assert _top_bid(tq, "no-token") == 0.52    # == NO best_bid


def test_join_touch_respects_min_edge(meta, profile):
    """贴盘口不得突破 min_edge 底线：min_edge_ticks=1 时买一(FV-0.5t) 会被挡掉。

    所以 join_touch_ticks 必须和 min_edge_ticks=0 成对使用，否则开关等于没开。
    """
    p = profile.with_overrides(
        {"delta_min_ticks": 1, "min_edge_ticks": 1, "join_touch_ticks": 1})
    tq = construct_quotes(_inputs(meta, p, **_one_tick_market()))
    assert _top_bid(tq, "yes-token") == 0.46   # 仍是买二，join 被 cap 挡回
    # 而且绝不会高于 FV - min_edge*tick
    assert _top_bid(tq, "yes-token") <= 0.475 - 1 * meta.tick_size


def test_join_touch_never_crosses_the_ask(meta, profile):
    """即使盘口锁死（买一 == 卖一，退化行情），贴盘口也不能挂到卖一上。"""
    p = profile.with_overrides(
        {"delta_min_ticks": 1, "min_edge_ticks": 0, "join_touch_ticks": 1})
    tq = construct_quotes(_inputs(meta, p, **_one_tick_market(yes_view=view(0.47, 0.47))))
    top = _top_bid(tq, "yes-token")
    assert top is not None
    assert top < 0.47  # 被 "never cross the ask" 拉回一档


def test_join_touch_with_wide_range_still_caps_at_best_bid(meta, profile):
    """join_touch_ticks 放宽的是"够得着"的门槛，不是报价本身 —— 最多只能到买一。"""
    p = profile.with_overrides(
        {"delta_min_ticks": 1, "min_edge_ticks": 0, "join_touch_ticks": 5})
    tq = construct_quotes(_inputs(meta, p, **_one_tick_market()))
    assert _top_bid(tq, "yes-token") == 0.47  # 买一，不会因为放宽就跑到中价去
