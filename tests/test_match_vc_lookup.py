"""Match VCs are created with a queue prefix ("Draft - Team 1"): the
formation helpers must find them with or without that prefix."""

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.match._cog import MatchCog
from cogs.match._formation import find_match_vc


def _vc(name: str, vc_id: int):
    vc = MagicMock()
    vc.name = name
    vc.id = vc_id
    return vc


def _category(*names: str):
    cat = MagicMock()
    cat.name = "Match #1"
    cat.voice_channels = [_vc(n, 100 + i) for i, n in enumerate(names)]
    return cat


def test_find_match_vc_with_queue_prefix():
    cat = _category("Draft - Team 1", "Draft - Team 2", "Draft - Waiting Match")
    assert find_match_vc(cat, "Team 1").name == "Draft - Team 1"
    assert find_match_vc(cat, "Waiting Match").name == "Draft - Waiting Match"


def test_find_match_vc_without_prefix():
    cat = _category("Team 1", "Team 2", "Waiting Match")
    assert find_match_vc(cat, "Team 2").name == "Team 2"


def test_find_match_vc_missing_returns_none():
    cat = _category("Draft - Team 1")
    assert find_match_vc(cat, "Waiting Match") is None
    # "Team 1" must not match a channel that merely contains it elsewhere.
    assert find_match_vc(_category("Team 1 bis"), "Team 1") is None


@pytest.mark.asyncio
async def test_move_players_to_prefixed_waiting_match():
    import bot as bot_module

    cat = _category("Draft - Team 1", "Draft - Team 2", "Draft - Waiting Match")
    waiting = cat.voice_channels[2]

    member = MagicMock()
    member.voice.channel.id = 1  # elsewhere
    member.move_to = AsyncMock()
    guild = MagicMock()
    guild.get_member = MagicMock(return_value=member)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog._move_players_to_waiting_match(guild, cat, ["7"])

    member.move_to.assert_awaited_once()
    assert member.move_to.await_args.args[0] is waiting
