"""Tests for the /stats cog: embeds + pagination + permission gate."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest


def _agg():
    return {
        "_id": "111:open",
        "user_id": "111",
        "queue_type": "open",
        "games": 47,
        "rounds_played": 1128,
        "kills": 1034,
        "deaths": 891,
        "assists": 312,
        "damage_made": 196443,
        "damage_received": 174022,
        "headshots": 781,
        "bodyshots": 2150,
        "legshots": 188,
        "multikills_2k": 142,
        "multikills_3k": 28,
        "multikills_4k": 7,
        "multikills_5k": 1,
        "first_kills": 198,
        "first_deaths": 156,
        "kast_rounds": 856,
        "rating_2_0_sum": 62.9,
        "updated_at": datetime.now(UTC),
    }


def _elo():
    return {
        "elo": 1547,
        "wins": 24,
        "losses": 13,
    }


def _member(name="Alice", member_id=111):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    avatar = MagicMock()
    avatar.url = "https://avatar"
    m.display_avatar = avatar
    return m


def test_overview_embed_contains_rating_and_kpr():
    from cogs.stats._embeds import build_overview_embed

    embed = build_overview_embed(
        elo_doc=_elo(),
        rank=12,
        agg=_agg(),
        member=_member(),
        queue_type="open",
    )
    text = repr(embed.to_dict())
    assert "Rating 2.0" in text
    assert "1547" in text
    assert "#12" in text
    # KPR = 1034 / 1128 = 0.9166...
    assert "0.92" in text or "0.91" in text


def test_details_embed_contains_multikills_and_opening():
    from cogs.stats._embeds import build_details_embed

    embed = build_details_embed(
        agg=_agg(),
        member=_member(),
        queue_type="open",
    )
    text = repr(embed.to_dict())
    assert "2K" in text and "142" in text
    assert "FK" in text and "198" in text
    assert "FD" in text and "156" in text


def test_overview_embed_with_no_aggregate_shows_elo_only_hint():
    from cogs.stats._embeds import build_overview_embed

    embed = build_overview_embed(
        elo_doc=_elo(),
        rank=12,
        agg=None,
        member=_member(),
        queue_type="open",
    )
    text = repr(embed.to_dict())
    assert "1547" in text
    assert "Les stats Rating 2.0 commencent à partir de votre prochain match." in text


@pytest.mark.asyncio
async def test_paginator_flips_to_details_on_next():
    from unittest.mock import AsyncMock

    import discord

    from cogs.stats._view import StatsPaginatorView

    overview = discord.Embed(title="overview")
    details = discord.Embed(title="details")
    view = StatsPaginatorView(
        overview=overview,
        details=details,
        invoker_id=999,
    )
    assert view.page == 0

    inter = MagicMock()
    inter.user.id = 999
    inter.response.edit_message = AsyncMock()

    next_button = next(c for c in view.children if getattr(c, "custom_id", "") == "stats_next")
    await next_button.callback(inter)

    assert view.page == 1
    args, kwargs = inter.response.edit_message.call_args
    assert kwargs["embed"] is details


@pytest.mark.asyncio
async def test_paginator_flips_back_to_overview_on_prev():
    from unittest.mock import AsyncMock

    import discord

    from cogs.stats._view import StatsPaginatorView

    overview = discord.Embed(title="overview")
    details = discord.Embed(title="details")
    view = StatsPaginatorView(
        overview=overview,
        details=details,
        invoker_id=999,
    )
    view.page = 1  # start on details

    inter = MagicMock()
    inter.user.id = 999
    inter.response.edit_message = AsyncMock()

    prev_button = next(c for c in view.children if getattr(c, "custom_id", "") == "stats_prev")
    await prev_button.callback(inter)

    assert view.page == 0
    kwargs = inter.response.edit_message.call_args.kwargs
    assert kwargs["embed"] is overview


@pytest.mark.asyncio
async def test_paginator_rejects_non_invoker():
    from unittest.mock import AsyncMock

    import discord

    from cogs.stats._view import StatsPaginatorView

    view = StatsPaginatorView(
        overview=discord.Embed(),
        details=discord.Embed(),
        invoker_id=111,
    )
    inter = MagicMock()
    inter.user.id = 222
    inter.response.send_message = AsyncMock()
    ok = await view.interaction_check(inter)
    assert ok is False
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_paginator_invoker_passes_interaction_check():
    import discord

    from cogs.stats._view import StatsPaginatorView

    view = StatsPaginatorView(
        overview=discord.Embed(),
        details=discord.Embed(),
        invoker_id=111,
    )
    inter = MagicMock()
    inter.user.id = 111
    ok = await view.interaction_check(inter)
    assert ok is True


@pytest.mark.asyncio
async def test_stats_command_sends_paginated_embed_when_agg_exists(monkeypatch):
    from unittest.mock import AsyncMock

    import bot as bot_module
    import cogs.stats._cog as stats_cog_module

    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_elo_col",
        lambda db: MagicMock(
            find_one=lambda *a, **k: _elo(),
            count_documents=lambda *a, **k: 11,
        ),
    )
    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_rating_aggregate",
        lambda db, *, user_id, queue_type: _agg(),
    )

    cog = stats_cog_module.StatsCog(bot_module.bot, bot_module.db)
    inter = MagicMock()
    inter.user = _member()
    inter.guild = MagicMock()
    inter.guild.name = "Guild"
    inter.response.send_message = AsyncMock()

    await cog.stats.callback(cog, inter, "open", None)

    inter.response.send_message.assert_awaited_once()
    kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in kwargs
    assert "view" in kwargs


@pytest.mark.asyncio
async def test_stats_command_empty_when_no_elo_doc(monkeypatch):
    from unittest.mock import AsyncMock

    import bot as bot_module
    import cogs.stats._cog as stats_cog_module

    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_elo_col",
        lambda db: MagicMock(find_one=lambda *a, **k: None),
    )
    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_rating_aggregate",
        lambda db, *, user_id, queue_type: None,
    )

    cog = stats_cog_module.StatsCog(bot_module.bot, bot_module.db)
    inter = MagicMock()
    inter.user = _member()
    inter.response.send_message = AsyncMock()

    await cog.stats.callback(cog, inter, "open", None)

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args.args[0]
    assert "n'a pas encore joué" in msg.lower()


@pytest.mark.asyncio
async def test_stats_command_no_aggregate_shows_single_page(monkeypatch):
    """ELO doc exists but no rating aggregate (pre-deployment match
    history): single-page embed, no view attached.
    """
    from unittest.mock import AsyncMock

    import bot as bot_module
    import cogs.stats._cog as stats_cog_module

    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_elo_col",
        lambda db: MagicMock(
            find_one=lambda *a, **k: _elo(),
            count_documents=lambda *a, **k: 5,
        ),
    )
    monkeypatch.setattr(
        stats_cog_module.repository,
        "get_rating_aggregate",
        lambda db, *, user_id, queue_type: None,
    )

    cog = stats_cog_module.StatsCog(bot_module.bot, bot_module.db)
    inter = MagicMock()
    inter.user = _member()
    inter.response.send_message = AsyncMock()

    await cog.stats.callback(cog, inter, "open", None)

    inter.response.send_message.assert_awaited_once()
    kwargs = inter.response.send_message.call_args.kwargs
    assert "embed" in kwargs
    assert "view" not in kwargs or kwargs.get("view") is None
