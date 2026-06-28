"""Tests for the riot_link cog: /link-riot, /unlink-riot, /refresh-elo."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from cogs.riot_link import RiotLinkCog
from services.riot_api import (
    Account,
    CurrentMMR,
    PlayerNotFoundError,
    RateLimitedError,
)


def _fake_member(member_id: int, name: str = "TestUser"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    return m


def _fake_interaction(user, guild_id: int = 42):
    inter = MagicMock()
    inter.user = user
    inter.guild_id = guild_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _fake_riot_client(*, account=None, mmr=None, history=None, raises=None):
    client = MagicMock()
    if raises:
        client.get_account.side_effect = raises
        client.get_current_mmr.side_effect = raises
        client.get_mmr_history.side_effect = raises
    else:
        client.get_account.return_value = account or Account(
            puuid="p1", name="X", tag="EUW", region="eu"
        )
        client.get_current_mmr.return_value = mmr or CurrentMMR(
            elo=1500, tier=14, tier_name="Platinum 3", ranking_in_tier=50, mmr_change_last=0
        )
        client.get_mmr_history.return_value = history or []
    return client


def _now() -> datetime:
    return datetime(2026, 4, 25, tzinfo=UTC)


# ── /link-riot ────────────────────────────────────────────────────
async def test_link_riot_invalid_format():
    import bot as bot_module

    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())

    user = _fake_member(1)
    inter = _fake_interaction(user)

    await cog.link_riot.callback(cog, inter, riot_id="no-tag-here")

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "Invalid format" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_link_riot_player_not_found():
    import bot as bot_module

    client = _fake_riot_client(raises=PlayerNotFoundError("nope"))
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)

    user = _fake_member(1)
    inter = _fake_interaction(user)

    await cog.link_riot.callback(cog, inter, riot_id="Ghost#404")

    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()
    args, kwargs = inter.followup.send.call_args
    assert "introuvable" in args[0]


async def test_link_riot_rate_limited():
    import bot as bot_module

    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client(raises=RateLimitedError()))

    inter = _fake_interaction(_fake_member(1))
    await cog.link_riot.callback(cog, inter, riot_id="X#1")

    args, _ = inter.followup.send.call_args
    assert "rate limit" in args[0]


async def test_link_riot_persists_metadata_without_seeding_elo():
    import bot as bot_module

    client = _fake_riot_client(
        account=Account(puuid="abc", name="Player", tag="EUW", region="eu"),
        mmr=CurrentMMR(
            elo=2450, tier=24, tier_name="Immortal 1", ranking_in_tier=50, mmr_change_last=0
        ),
        # 6-month history empty -> the computed peak falls back to the
        # current MMR elo (2450), making the persisted peak deterministic.
        history=[],
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    from services import repository

    doc = repository.get_riot_account(bot_module.db, 1)
    assert doc is not None
    assert doc["riot_name"] == "Player"
    assert doc["riot_tag"] == "EUW"
    assert doc["riot_region"] == "eu"
    assert doc["puuid"] == "abc"
    # /link-riot now computes a 6-month peak ELO and persists it under the
    # "peak_6mo" source. With an empty history the peak equals the current
    # MMR elo (2450).
    assert doc["source"] == "peak_6mo"
    assert doc["peak_elo"] == 2450

    # No ELO seed: the shared `elo` collection stays empty until the
    # player has played in a queue.
    assert repository.get_elo_col(bot_module.db).count_documents({}) == 0

    embed = inter.followup.send.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Riot ID"] == "**Player#EUW**"
    # No "Server ELO" field: we no longer expose a number after linking.
    assert "Server ELO" not in fields


async def test_link_riot_accepts_any_rank():
    """No rank gate-keeping: link without any Riot rank condition."""
    import bot as bot_module

    client = _fake_riot_client(
        account=Account(puuid="abc", name="Iron", tag="EUW", region="eu"),
        mmr=CurrentMMR(elo=300, tier=3, tier_name="Iron 3", ranking_in_tier=0, mmr_change_last=0),
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "IronPlayer"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Iron#EUW")

    from services import repository

    assert repository.get_riot_account(bot_module.db, 1) is not None
    # No ELO seed even for an Iron: we only persist metadata.
    assert repository.get_elo_col(bot_module.db).count_documents({}) == 0


async def test_link_riot_does_not_touch_existing_elo():
    """If an ELO doc already exists (other queue, previous match), /link
    does not modify it. The link does not affect accumulated ELOs."""
    import bot as bot_module
    from services import repository

    # Existing ELO in the Open queue (compound _id <user>:open)
    repository.get_elo_col(bot_module.db).insert_one(
        {
            "_id": "1:open",
            "user_id": "1",
            "queue_type": "open",
            "name": "Jet",
            "elo": 2200,
            "wins": 5,
            "losses": 2,
        }
    )

    client = _fake_riot_client(
        account=Account(puuid="abc", name="Player", tag="EUW", region="eu"),
        mmr=CurrentMMR(
            elo=2450, tier=24, tier_name="Immortal 1", ranking_in_tier=50, mmr_change_last=0
        ),
    )
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    # The existing ELO doc did not move.
    elo_doc = repository.get_elo_col(bot_module.db).find_one({"_id": "1:open"})
    assert elo_doc["elo"] == 2200
    assert elo_doc["wins"] == 5
    assert elo_doc["losses"] == 2


async def test_link_unlink_relink_does_not_change_elo():
    """/link, /unlink, /link has no impact on the accumulated ELO."""
    import bot as bot_module
    from services import repository

    repository.get_elo_col(bot_module.db).insert_one(
        {
            "_id": "1:open",
            "user_id": "1",
            "queue_type": "open",
            "name": "Jet",
            "elo": 2200,
            "wins": 5,
            "losses": 2,
        }
    )

    client = _fake_riot_client()
    cog = RiotLinkCog(bot_module.bot, bot_module.db, client)
    inter = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")
    assert repository.get_elo_col(bot_module.db).find_one({"_id": "1:open"})["elo"] == 2200

    await cog.unlink_riot.callback(cog, _fake_interaction(_fake_member(1), guild_id=42))
    assert repository.get_elo_col(bot_module.db).find_one({"_id": "1:open"})["elo"] == 2200

    inter2 = _fake_interaction(_fake_member(1, "Jet"), guild_id=42)
    await cog.link_riot.callback(cog, inter2, riot_id="Player#EUW")
    assert repository.get_elo_col(bot_module.db).find_one({"_id": "1:open"})["elo"] == 2200


# ── /unlink-riot ──────────────────────────────────────────────────
async def test_unlink_riot_when_linked():
    import bot as bot_module
    from services import repository

    repository.link_riot_account(
        bot_module.db,
        user_id=1,
        riot_name="X",
        riot_tag="1",
        riot_region="eu",
        puuid="abc",
        peak_elo=1500,
        source="peak_recent",
    )

    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())
    inter = _fake_interaction(_fake_member(1), guild_id=42)
    await cog.unlink_riot.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "délié" in args[0].lower()
    assert repository.get_riot_account(bot_module.db, 1) is None


async def test_unlink_riot_when_not_linked():
    import bot as bot_module

    cog = RiotLinkCog(bot_module.bot, bot_module.db, _fake_riot_client())
    inter = _fake_interaction(_fake_member(99), guild_id=42)
    await cog.unlink_riot.callback(cog, inter)

    args, _ = inter.response.send_message.call_args
    assert "Aucun compte Riot lié" in args[0]
