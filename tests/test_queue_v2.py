"""Tests for the queue_v2 cog + repository queue."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.queue_v2 import (
    QueueCog,
    QueueView,
    build_queue_embed,
)
from services import repository


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.voice = None
    # Explicit async methods - required because __class__=discord.Member
    # below freezes the type and prevents auto-creation of AsyncMock.
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.move_to = AsyncMock()
    # Mark as discord.Member to pass the fail-safe isinstance check
    # in _join_callback (role gate). cast to ignore the MagicMock vs
    # Member type incompatibility from mypy's perspective.
    m.__class__ = discord.Member  # type: ignore[assignment]
    return m


def _fake_guild(guild_id: int = 42, name: str = "TestGuild"):
    g = MagicMock()
    g.id = guild_id
    g.name = name
    g.roles = []
    g.voice_channels = []
    g.get_channel = MagicMock(return_value=None)
    return g


def _fake_interaction(
    user,
    guild_id: int = 42,
    channel_name: str = "open-queue",
):
    inter = MagicMock()
    inter.user = user
    inter.guild = _fake_guild(guild_id)
    inter.guild_id = guild_id
    inter.channel_id = 100
    inter.channel = MagicMock()
    inter.channel.id = 100
    inter.channel.name = channel_name
    inter.channel.guild = inter.guild
    inter.channel.send = AsyncMock(return_value=MagicMock(id=999))
    inter.channel.mention = "#general"
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.message = MagicMock()
    inter.message.id = 999
    return inter


def _make_rank_role(name: str):
    r = MagicMock()
    r.name = name
    return r


def _give_queue_access(member, queue_type: str) -> None:
    """Attach the role required to pass the gate for `queue_type`.

    The Open queue is gated by the "Members" role; the Advanced queue is
    gated by the "Rank Q | Advanced Queue" role.
    """
    role_map = {
        "open": "Members",
        "advanced": "Rank Q | Advanced Queue",
    }
    role_name = role_map[queue_type]
    if role_name is None:
        return
    existing = list(getattr(member, "roles", []) or [])
    existing.append(_make_rank_role(role_name))
    member.roles = existing


def _seed_riot_link(db, guild_id: int, user_id: int, elo: int = 1500):
    repository.link_riot_account(
        db,
        user_id=user_id,
        riot_name=f"P{user_id}",
        riot_tag="EUW",
        riot_region="eu",
        puuid=f"pu-{user_id}",
        peak_elo=elo,
        source="peak_recent",
    )
    # Compound _id matching the new per-queue architecture.
    repository.get_elo_col(db).insert_one(
        {
            "_id": f"{user_id}:open",
            "name": f"P{user_id}",
            "elo": elo,
            "wins": 0,
            "losses": 0,
            "queue_type": "open",
            "user_id": str(user_id),
        }
    )


def _seed_active_queue(db, guild_id: int = 42, queue_type: str = "open"):
    repository.setup_active_queue(
        db,
        guild_id=guild_id,
        queue_type=queue_type,
        channel_id=100,
        message_id=999,
    )


# -- Repository: add/remove --
def test_repo_add_player_to_no_queue():
    import bot as bot_module

    res = repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert not res.success
    assert res.reason == "no_queue"


def test_repo_add_player_success():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    res = repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert res.success
    assert res.reason == "added"
    assert "1" in res.queue["players"]


def test_repo_add_player_already_in():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    res = repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert not res.success
    assert res.reason == "already_in"


def test_repo_add_player_queue_full():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    for i in range(10):
        repository.add_player_to_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
            user_id=i,
        )
    res = repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=99,
    )
    assert not res.success
    assert res.reason == "queue_full"


def test_repo_add_player_when_closed():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    repository.close_active_queue(bot_module.db, guild_id=42, queue_type="open")
    res = repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert not res.success
    assert res.reason == "queue_closed"


def test_repo_remove_player_not_in():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    res = repository.remove_player_from_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert not res.success
    assert res.reason == "not_in"


def test_repo_remove_player_success():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    res = repository.remove_player_from_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    assert res.success
    assert res.reason == "removed"
    assert "1" not in res.queue["players"]


def test_repo_delete_active_queue():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    assert (
        repository.delete_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is True
    )
    assert (
        repository.get_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is None
    )


# -- Embed --
def test_embed_empty_queue():
    embed = build_queue_embed(None, _fake_guild(), "open")
    assert "0/10" in embed.title
    assert any("Personne" in f.value for f in embed.fields)


def test_embed_with_players():
    doc = {"players": ["1", "2", "3"], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "3/10" in embed.title
    field_value = next(f.value for f in embed.fields if f.name == "Joueurs")
    assert "<@1>" in field_value
    assert "<@2>" in field_value


def test_embed_full_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "open"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "10/10" in embed.title
    assert "pleine" in embed.description.lower()


def test_embed_forming_queue():
    doc = {"players": [str(i) for i in range(10)], "status": "forming"}
    embed = build_queue_embed(doc, _fake_guild(), "open")
    assert "formation" in embed.description.lower()


def test_embed_title_per_queue_type():
    """Every queue_type displays its label in the title."""
    g = _fake_guild()
    open_embed = build_queue_embed(None, g, "open")
    advanced_embed = build_queue_embed(None, g, "advanced")
    assert "Open Queue" in open_embed.title
    assert "Advanced Queue" in advanced_embed.title


# -- Join button --
async def test_join_without_riot_account_refuses():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)

    await view._join_callback(inter)

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "Riot" in args[0]
    assert kwargs.get("ephemeral") is True
    inter.edit_original_response.assert_not_awaited()


async def test_join_no_active_queue_refuses():
    import bot as bot_module

    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)

    await view._join_callback(inter)
    args, _ = inter.followup.send.call_args
    assert "Aucune file active" in args[0]


async def test_join_success_updates_message():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1, "Jet")
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)

    await view._join_callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "1/10" in embed.title


async def test_join_success_sends_ephemeral_confirmation():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1, "Jet")
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)

    await view._join_callback(inter)

    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    msg = args[0]
    assert "rejoint" in msg.lower()
    assert "1/10" in msg
    assert kwargs.get("ephemeral") is True
    inter.channel.send.assert_not_awaited()


async def test_join_already_in_refuses():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )

    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    args, _ = inter.followup.send.call_args
    assert "déjà dans la file" in args[0].lower()


async def test_join_refused_when_player_in_active_match():
    """A player still engaged in a match (active Discord category, ELO
    not applied) cannot join a new queue."""
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)

    bot_module.db["matches"].insert_one(
        {
            "team_a": [{"id": 1, "name": "Jet", "elo": 1500}],
            "team_b": [{"id": 2, "name": "Sage", "elo": 1500}],
            "map": "Bind",
            "queue_type": "open",
            "origin_guild_id": 42,
            "status": "pending",
            "match_number": 7,
            "category_id": 999,
            "votes": {},
        }
    )

    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    args, _ = inter.followup.send.call_args
    assert "match en cours" in args[0]
    assert "Match #7" in args[0]


async def test_join_allowed_when_match_validated_even_without_elo_applied():
    """Once the match is validated by vote (validated_a / validated_b), the
    player must be able to re-queue immediately, even if the ELO has not
    been applied yet. Otherwise a Henrik outage (private tracker, wrong
    account played, API down) blocks the 10 players indefinitely. The
    ELO distribution becomes an async job independent from the gate."""
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)

    bot_module.db["matches"].insert_one(
        {
            "team_a": [{"id": 1, "name": "Jet", "elo": 1500}],
            "team_b": [{"id": 2, "name": "Sage", "elo": 1500}],
            "map": "Bind",
            "queue_type": "open",
            "origin_guild_id": 42,
            "status": "validated_a",
            "elo_applied": False,
            "match_number": 11,
            "category_id": 999,
            "votes": {},
        }
    )

    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    # The player must be authorized: no "ongoing match" message.
    if inter.followup.send.called:
        args, _ = inter.followup.send.call_args
        assert "ongoing match" not in args[0]
    inter.edit_original_response.assert_awaited_once()


async def test_join_allowed_when_match_elo_already_applied():
    """A match whose ELO has already been applied must not block the
    queue: the category has been deleted and the player can play again."""
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)

    bot_module.db["matches"].insert_one(
        {
            "team_a": [{"id": 1, "name": "Jet", "elo": 1500}],
            "team_b": [{"id": 2, "name": "Sage", "elo": 1500}],
            "map": "Bind",
            "queue_type": "open",
            "origin_guild_id": 42,
            "status": "validated_a",
            "elo_applied": True,
            "votes": {},
        }
    )

    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    inter.edit_original_response.assert_awaited_once()


async def test_match_replace_releases_quitter_and_blocks_replacement():
    """After /match-replace: the leaver must be able to re-queue (they are
    no longer in team_a/team_b) and the replacement must be blocked (they
    are now there). Locks the invariant between
    find_active_match_for_player and the atomic $set mutation on the team
    array."""
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=99)

    matches = bot_module.db["matches"]
    matches.insert_one(
        {
            "team_a": [{"id": 1, "name": "Jet", "elo": 1500}],
            "team_b": [{"id": 2, "name": "Sage", "elo": 1500}],
            "map": "Bind",
            "queue_type": "open",
            "origin_guild_id": 42,
            "status": "pending",
            "match_number": 8,
            "category_id": 999,
            "votes": {},
        }
    )

    assert repository.find_active_match_for_player(bot_module.db, 1) is not None
    assert repository.find_active_match_for_player(bot_module.db, 99) is None

    # Simulates /match-replace: atomically replaces the content of team_a.
    # Cf. cogs/match/_cog.py match_replace -> update_one({"_id":...,
    # "status":"pending"}, {"$set": {team_key: new_team}}).
    matches.update_one(
        {"status": "pending", "match_number": 8},
        {"$set": {"team_a": [{"id": 99, "name": "Phx", "elo": 1500}]}},
    )

    assert repository.find_active_match_for_player(bot_module.db, 1) is None
    assert repository.find_active_match_for_player(bot_module.db, 99) is not None


async def test_join_10th_player_triggers_on_full():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    for i in range(10):
        _seed_riot_link(bot_module.db, guild_id=42, user_id=i, elo=1500 + i * 50)
    # 9 already in queue
    for i in range(9):
        repository.add_player_to_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
            user_id=i,
        )

    triggered = []

    async def on_full(inter, queue_doc, queue_type):
        triggered.append((queue_doc, queue_type))

    view = QueueView(bot_module.db, queue_type="open", on_full=on_full)
    member = _fake_member(9)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    # Give the task a chance to run
    import asyncio

    await asyncio.sleep(0)

    assert len(triggered) == 1
    queue_doc, queue_type = triggered[0]
    assert len(queue_doc["players"]) == 10
    assert queue_type == "open"
    # The queue moved to status "forming"
    queue = repository.get_active_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
    )
    assert queue["status"] == "forming"


async def test_join_when_queue_forming_refuses():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.close_active_queue(bot_module.db, guild_id=42, queue_type="open")

    view = QueueView(bot_module.db, queue_type="open")
    member = _fake_member(1)
    _give_queue_access(member, "open")
    inter = _fake_interaction(member)
    await view._join_callback(inter)

    args, _ = inter.followup.send.call_args
    assert "fermée" in args[0].lower()


# -- Leave button --
async def test_leave_when_not_in_queue_refuses():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))

    await view._leave_callback(inter)
    args, _ = inter.followup.send.call_args
    assert "pas dans la file" in args[0].lower()


async def test_leave_success_updates_message():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )

    view = QueueView(bot_module.db, queue_type="open")
    inter = _fake_interaction(_fake_member(1))
    await view._leave_callback(inter)

    inter.edit_original_response.assert_awaited_once()
    embed = inter.edit_original_response.call_args.kwargs["embed"]
    assert "0/10" in embed.title


# -- /setup-queue --
async def test_setup_queue_creates_active_queue():
    import bot as bot_module

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    await cog.setup_queue.callback(cog, inter, queue="open")

    inter.channel.send.assert_awaited_once()
    queue = repository.get_active_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
    )
    assert queue is not None
    assert queue["channel_id"] == 100
    assert queue["message_id"] == 999
    assert queue["status"] == "open"
    assert queue["players"] == []


async def test_setup_queue_replaces_existing():
    import bot as bot_module

    _seed_active_queue(bot_module.db)

    # Add a player to old queue
    _seed_riot_link(bot_module.db, guild_id=42, user_id=1)
    repository.add_player_to_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    old = repository.get_active_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
    )
    assert "1" in old["players"]

    # Re-setup
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.setup_queue.callback(cog, inter, queue="open")

    new = repository.get_active_queue(
        bot_module.db,
        guild_id=42,
        queue_type="open",
    )
    assert new["players"] == []  # reset


# -- /close-queue --
async def test_close_queue_when_active():
    import bot as bot_module

    _seed_active_queue(bot_module.db)
    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "supprimée" in args[0].lower()
    assert (
        repository.get_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is None
    )


async def test_close_queue_when_no_queue():
    import bot as bot_module

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))
    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "Aucune file" in args[0]


async def test_setup_queue_rejects_wrong_channel():
    """/setup-queue open in a channel other than #open-queue is refused."""
    import bot as bot_module

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(
        _fake_member(99),
        channel_name="general",
    )

    await cog.setup_queue.callback(cog, inter, queue="open")

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "open-queue" in args[0]
    assert kwargs.get("ephemeral") is True
    inter.channel.send.assert_not_awaited()
    assert (
        repository.get_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is None
    )


async def test_setup_queue_rejects_advanced_in_open_channel():
    """/setup-queue advanced in #open-queue is refused (wrong queue type
    for the channel: advanced must be set up in #advanced-queue)."""
    import bot as bot_module

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(
        _fake_member(99),
        channel_name="open-queue",
    )

    await cog.setup_queue.callback(cog, inter, queue="advanced")

    args, _ = inter.response.send_message.call_args
    assert "advanced-queue" in args[0]
    inter.channel.send.assert_not_awaited()


async def test_close_queue_deletes_persistent_message():
    """/close-queue deletes the Join/Leave message in Discord."""
    import bot as bot_module

    _seed_active_queue(bot_module.db)

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    fake_channel = MagicMock()
    fake_channel.fetch_message = AsyncMock(return_value=fake_msg)
    inter.guild.get_channel = MagicMock(return_value=fake_channel)

    await cog.close_queue.callback(cog, inter, queue="open")

    inter.guild.get_channel.assert_called_once_with(100)
    fake_channel.fetch_message.assert_awaited_once_with(999)
    fake_msg.delete.assert_awaited_once()
    assert (
        repository.get_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is None
    )


async def test_close_queue_tolerates_missing_message():
    """If the message has already been deleted on the Discord side,
    /close-queue does not crash and still removes the queue from the
    DB."""
    import discord as _discord

    import bot as bot_module

    _seed_active_queue(bot_module.db)

    cog = QueueCog(bot_module.bot, bot_module.db)
    inter = _fake_interaction(_fake_member(99))

    fake_channel = MagicMock()
    fake_channel.fetch_message = AsyncMock(
        side_effect=_discord.NotFound(MagicMock(status=404), "gone"),
    )
    inter.guild.get_channel = MagicMock(return_value=fake_channel)

    await cog.close_queue.callback(cog, inter, queue="open")

    args, _ = inter.response.send_message.call_args
    assert "supprimée" in args[0].lower()
    assert (
        repository.get_active_queue(
            bot_module.db,
            guild_id=42,
            queue_type="open",
        )
        is None
    )


# -- Button custom IDs (for persistence) --
async def test_button_custom_ids_per_queue_type():
    """The custom_ids carry the queue_type to allow the 4 persistent
    messages to coexist after a bot restart."""
    db = MagicMock()
    open_view = QueueView(db, queue_type="open")
    advanced = QueueView(db, queue_type="advanced")
    assert open_view.join_btn.custom_id == "queue_v2:join:open"
    assert open_view.leave_btn.custom_id == "queue_v2:leave:open"
    assert advanced.join_btn.custom_id == "queue_v2:join:advanced"
    assert advanced.leave_btn.custom_id == "queue_v2:leave:advanced"


# -- Tests Task 9: 4-queue system --
async def test_join_advanced_queue_requires_role():
    """Without the 'Rank Q | Advanced Queue' role, joining the Advanced
    Queue is refused."""
    import discord

    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="advanced",
        channel_id=100,
        message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []  # no Advanced Queue role
    member.__class__ = discord.Member
    inter = _fake_interaction(member)
    inter.user = member

    view = QueueView(db, queue_type="advanced")
    await view._join_callback(inter)

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "Rank Q | Advanced Queue" in msg
    assert "réservée" in msg
    # The player must NOT have been added to the gated queue.
    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" not in doc["players"]


async def test_join_advanced_queue_allowed_with_role():
    """With the 'Rank Q | Advanced Queue' role, joining is allowed."""
    import discord

    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="advanced",
        channel_id=100,
        message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = [_make_rank_role("Rank Q | Advanced Queue")]
    member.__class__ = discord.Member
    inter = _fake_interaction(member, channel_name="advanced-queue")
    inter.user = member

    view = QueueView(db, queue_type="advanced")
    await view._join_callback(inter)

    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" in doc["players"]


async def test_join_open_queue_requires_members_role():
    """Open Queue is gated by the "Members" role: a linked player holding
    that role (granted on candidature accept) can join."""
    import discord

    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="open",
        channel_id=100,
        message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    _give_queue_access(member, "open")  # holds the "Members" role
    member.__class__ = discord.Member
    inter = _fake_interaction(member, channel_name="open-queue")
    inter.user = member

    view = QueueView(db, queue_type="open")
    await view._join_callback(inter)

    doc = repository.get_active_queue(db, 42, "open")
    assert "1" in doc["players"]


async def test_join_open_queue_refused_without_members_role():
    """Open Queue rejects a linked player who has not been accepted
    (no "Members" role)."""
    import discord

    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="open",
        channel_id=100,
        message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []  # no role at all
    member.__class__ = discord.Member
    inter = _fake_interaction(member, channel_name="open-queue")
    inter.user = member

    view = QueueView(db, queue_type="open")
    await view._join_callback(inter)

    # Refused: the gate role is missing, so the player is not enqueued.
    doc = repository.get_active_queue(db, 42, "open")
    assert doc is None or "1" not in doc["players"]


async def test_cannot_join_two_queues_simultaneously():
    """If already in the Open Queue, joining the Advanced Queue is refused."""
    import discord

    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="open",
        channel_id=100,
        message_id=999,
    )
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="advanced",
        channel_id=200,
        message_id=888,
    )
    repository.add_player_to_queue(
        db,
        guild_id=42,
        queue_type="open",
        user_id=1,
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    _give_queue_access(member, "advanced")
    member.__class__ = discord.Member
    inter = _fake_interaction(member)
    inter.user = member

    view_advanced = QueueView(db, queue_type="advanced")
    await view_advanced._join_callback(inter)

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    # Cross-queue refusal: already in OPEN, cannot join ADVANCED.
    assert "déjà dans la file **OPEN**" in msg
    # And the player was not added to the advanced queue.
    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" not in doc["players"]


@pytest.mark.asyncio
async def test_join_rejects_non_member_user():
    """If inter.user is not a Member, the join is refused (fail-safe)."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db,
        guild_id=42,
        queue_type="open",
        channel_id=100,
        message_id=999,
    )
    _seed_riot_link(db, 42, 1)

    user = MagicMock()  # Not a discord.Member
    user.id = 1
    user.display_name = "User"
    user.mention = "<@1>"
    # IMPORTANT: do NOT set user.__class__ = discord.Member
    inter = _fake_interaction(user)
    inter.user = user

    view = QueueView(db, queue_type="open")
    await view._join_callback(inter)

    # The join must be refused with a clear message
    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "invalid" in msg.lower() or "server" in msg.lower()


def test_waiting_room_name_per_queue_type():
    from cogs.queue_v2 import WAITING_ROOM_NAMES

    assert WAITING_ROOM_NAMES["open"] == "Waiting Room Open"
    assert WAITING_ROOM_NAMES["advanced"] == "Waiting Room Advanced"


def test_queue_role_gates_per_queue_type():
    """Snapshot of QUEUE_ROLE_GATES: Open is gated by the "Members" role,
    Advanced is gated by the "Rank Q | Advanced Queue" role. FL CAST is NOT
    a queue gate — casters access match channels via MATCH_VIEWER_ROLE_NAMES."""
    from cogs.queue_v2 import QUEUE_ROLE_GATES

    assert QUEUE_ROLE_GATES["open"] == ("Members",)
    assert QUEUE_ROLE_GATES["advanced"] == ("Rank Q | Advanced Queue",)


def test_queue_channel_names_per_queue_type():
    from cogs.queue_v2 import QUEUE_CHANNEL_NAMES

    assert QUEUE_CHANNEL_NAMES["open"] == "open-queue"
    assert QUEUE_CHANNEL_NAMES["advanced"] == "advanced-queue"
    assert QUEUE_CHANNEL_NAMES["open"] == "open-queue"
    assert QUEUE_CHANNEL_NAMES["advanced"] == "advanced-queue"


def test_queue_labels_per_queue_type():
    from cogs.queue_v2 import QUEUE_LABELS

    assert QUEUE_LABELS["open"] == "Open Queue"
    assert QUEUE_LABELS["advanced"] == "Advanced Queue"


def test_queue_role_name_is_en_queue():
    from cogs.queue_v2 import QUEUE_ROLE_NAME

    assert QUEUE_ROLE_NAME == "En Queue"


async def test_join_advanced_queue_refused_with_unrelated_staff_role():
    """Advanced Queue: an unrelated staff role (e.g. 'FL STAFF') does not
    satisfy the 'Rank Q | Advanced Queue' gate, so the join is refused."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="advanced", channel_id=100, message_id=999
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = [_make_rank_role("FL STAFF")]
    inter = _fake_interaction(member, channel_name="advanced-queue")
    inter.user = member

    view = QueueView(db, queue_type="advanced")
    await view._join_callback(inter)

    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" not in doc["players"]


async def test_join_semipro_queue_refused_with_fl_staff_semipro_role():
    """Semi Pro Queue: FL STAFF SEMIPRO alone is no longer a queue gate."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="advanced", channel_id=100, message_id=999
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = [_make_rank_role("FL STAFF SEMIPRO")]
    inter = _fake_interaction(member, channel_name="advanced-queue")
    inter.user = member

    view = QueueView(db, queue_type="advanced")
    await view._join_callback(inter)

    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" not in doc["players"]


async def test_join_gc_queue_refused_with_fl_staff_gc_role():
    """GC Queue: FL STAFF GC alone is no longer a queue gate."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView

    db = bot_module.db
    repository.setup_active_queue(
        db, guild_id=42, queue_type="advanced", channel_id=100, message_id=999
    )
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = [_make_rank_role("FL STAFF GC")]
    inter = _fake_interaction(member, channel_name="advanced-queue")
    inter.user = member

    view = QueueView(db, queue_type="advanced")
    await view._join_callback(inter)

    doc = repository.get_active_queue(db, 42, "advanced")
    assert "1" not in doc["players"]


def test_viewer_roles_are_not_queue_gates():
    """Staff/viewer roles (MATCH_VIEWER_ROLE_NAMES) can see match channels
    and join voice rooms, but are NOT in QUEUE_ROLE_GATES: holding a viewer
    role alone must not let someone queue up as a player."""
    from cogs.match._constants import MATCH_VIEWER_ROLE_NAMES
    from cogs.queue_v2 import QUEUE_ROLE_GATES

    assert MATCH_VIEWER_ROLE_NAMES  # there is at least one viewer role
    for queue_type, gate in QUEUE_ROLE_GATES.items():
        if gate is None:
            continue  # ungated queue has no role gate to check
        for viewer_role in MATCH_VIEWER_ROLE_NAMES:
            assert viewer_role not in gate, f"{viewer_role} must not gate the {queue_type} queue"
