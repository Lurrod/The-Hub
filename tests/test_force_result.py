"""Tests for /match-force-result and repository.force_match_result_atomically.

An admin uses this to settle a vote that timed out (`contested`) — or
pre-empt a still-open `pending` vote — without the 7/10 majority.
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.match import MatchCog
from services import repository


def _seed_match(db, *, channel_id: int = 100, message_id: int = 555):
    return repository.create_match(
        db,
        origin_guild_id=42,
        team_a=[{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        map_name="Ascent",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=message_id,
        channel_id=channel_id,
        queue_type="open",
    )


# ── Repository: force_match_result_atomically ─────────────────────────────


def test_force_contested_match_to_validated_a():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "contested")

    forced = repository.force_match_result_atomically(bot_module.db, channel_id=100, winner="a")

    assert forced is not None
    assert forced["status"] == "validated_a"
    assert forced.get("validated_at") is not None
    stored = repository.get_match(bot_module.db, match_id)
    assert stored["status"] == "validated_a"


def test_force_pending_match_to_validated_b():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)

    forced = repository.force_match_result_atomically(bot_module.db, channel_id=100, winner="b")

    assert forced is not None
    assert forced["status"] == "validated_b"
    stored = repository.get_match(bot_module.db, match_id)
    assert stored["status"] == "validated_b"


def test_force_rejects_already_validated_match():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    forced = repository.force_match_result_atomically(bot_module.db, channel_id=100, winner="b")

    assert forced is None
    # Status untouched: no overwrite of an already-settled result.
    assert repository.get_match(bot_module.db, match_id)["status"] == "validated_a"


def test_force_rejects_when_elo_already_applied():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "contested")
    bot_module.db["matches"].update_one({"_id": match_id}, {"$set": {"elo_applied": True}})

    forced = repository.force_match_result_atomically(bot_module.db, channel_id=100, winner="a")

    assert forced is None


def test_force_returns_none_when_no_match_in_channel():
    import bot as bot_module

    _seed_match(bot_module.db, channel_id=100)

    forced = repository.force_match_result_atomically(bot_module.db, channel_id=999, winner="a")

    assert forced is None


def test_force_invalid_winner_raises():
    import bot as bot_module

    _seed_match(bot_module.db)

    with pytest.raises(ValueError):
        repository.force_match_result_atomically(bot_module.db, channel_id=100, winner="draw")


# ── Cog command: match_force_result ───────────────────────────────────────


def _fake_choice(value: str):
    choice = MagicMock()
    choice.value = value
    return choice


def _fake_interaction(channel_id: int = 100):
    inter = MagicMock()
    inter.channel_id = channel_id
    inter.guild = MagicMock()
    inter.guild.name = "TestGuild"
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.mark.asyncio
async def test_force_result_command_no_match():
    import bot as bot_module

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    inter = _fake_interaction(channel_id=100)

    await cog.match_force_result.callback(cog, inter, _fake_choice("a"))

    inter.response.defer.assert_awaited_once()
    args, kwargs = inter.followup.send.await_args
    assert "Aucun match forçable" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_force_result_command_happy_path(monkeypatch):
    import bot as bot_module
    import cogs.match._verification as match_cog_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    match_id = _seed_match(bot_module.db, channel_id=100)
    repository.set_match_status(bot_module.db, match_id, "contested")
    bot_module.db["matches"].update_one({"_id": match_id}, {"$set": {"category_id": 5555}})

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    inter = _fake_interaction(channel_id=100)
    # _on_match_validated looks up the #elo-adding channel; none here.
    inter.guild.text_channels = []

    await cog.match_force_result.callback(cog, inter, _fake_choice("a"))

    # Match transitioned to validated_a.
    assert repository.get_match(bot_module.db, match_id)["status"] == "validated_a"
    # Confirmation sent to the admin.
    args, _ = inter.followup.send.await_args
    assert "Team A a gagné" in args[0]
    # Standard post-validation hook fired: category torn down.
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 5555
