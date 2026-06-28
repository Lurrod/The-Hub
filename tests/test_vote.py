"""Tests for the voting system (Phase 5)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from cogs.match import (
    MAJORITY_THRESHOLD,
    VOTE_TIMEOUT_MINUTES,
    MatchCog,
    VoteView,
    build_match_embed_from_doc,
)
from cogs.match._constants import (
    HENRIK_VERIFY_TIMEOUT_MINUTES,
)
from services import repository


def _fake_member(member_id: int, name: str = "User"):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    return m


def _fake_guild(guild_id: int = 42, roles=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.roles = roles or []
    g.get_channel = lambda cid: channel
    g.get_member = lambda uid: None  # default: leader/players not resolved
    if channel is not None:
        channel.name = "elo-adding"
        g.text_channels = [channel]
    else:
        g.text_channels = []
    return g


def _fake_interaction(user, guild, message_id: int = 555):
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.message = MagicMock()
    inter.message.id = message_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    return inter


def _seed_match(
    db, guild_id: int = 42, message_id: int = 555, team_a_ids=range(0, 5), team_b_ids=range(5, 10)
):
    return repository.create_match(
        db,
        origin_guild_id=guild_id,
        team_a=[{"id": i, "name": f"P{i}", "elo": 1500 + i * 50} for i in team_a_ids],
        team_b=[{"id": i, "name": f"P{i}", "elo": 1500 + i * 50} for i in team_b_ids],
        map_name="Ascent",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=message_id,
        channel_id=100,
        queue_type="open",
    )


# -- Vote: refusals --
async def test_vote_when_no_match_for_message():
    import bot as bot_module

    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(0), _fake_guild(), message_id=999)

    await view.vote_a.callback(inter)

    args, kwargs = inter.response.send_message.call_args
    assert "introuvable" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_vote_when_user_did_not_play_match_refused():
    import bot as bot_module

    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)
    # User 99 did not play
    inter = _fake_interaction(_fake_member(99), _fake_guild())

    await view.vote_a.callback(inter)

    args, _ = inter.response.send_message.call_args
    assert "n'as pas joué" in args[0]
    inter.response.edit_message.assert_not_awaited()


async def test_vote_on_validated_match_refused():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(0), _fake_guild())
    await view.vote_a.callback(inter)

    args, _ = inter.response.send_message.call_args
    assert "déjà validé" in args[0]


# -- Vote: recording --
async def test_vote_recorded_in_db():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)

    view = VoteView(bot_module.db)
    inter = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, match_id)
    assert match["votes"] == {"3": "a"}
    assert match["status"] == "pending"
    inter.response.edit_message.assert_awaited_once()


async def test_vote_can_be_changed():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    # User 3 votes A
    inter1 = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_a.callback(inter1)
    # Then switches to B
    inter2 = _fake_interaction(_fake_member(3), _fake_guild())
    await view.vote_b.callback(inter2)

    match = repository.get_match(bot_module.db, match_id)
    assert match["votes"] == {"3": "b"}


# -- Majority --
async def test_six_votes_for_a_keeps_pending():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    for uid in range(6):  # players 0..5 vote A (6 votes)
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "pending"
    a_count = sum(1 for v in match["votes"].values() if v == "a")
    assert a_count == 6


async def test_seven_votes_for_a_validates_match():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)

    triggered = []

    async def on_validated(inter, match_doc):
        triggered.append(match_doc)

    view = VoteView(bot_module.db, on_validated=on_validated)

    for uid in range(MAJORITY_THRESHOLD):  # 7 players vote A
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "validated_a"
    assert match["validated_at"] is not None

    # on_validated was called only once (on the 7th vote)
    assert len(triggered) == 1
    assert triggered[0]["status"] == "validated_a"


async def test_seven_votes_for_b_validates_b():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_b.callback(inter)

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "validated_b"


async def test_validated_view_removed_from_message():
    """After validation: view=None is passed to edit_message (buttons removed)."""
    import bot as bot_module

    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    inter_last = None
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), _fake_guild())
        await view.vote_a.callback(inter)
        inter_last = inter

    last_call = inter_last.response.edit_message.call_args
    assert last_call.kwargs["view"] is None


# -- Embed: reflects the votes --
async def test_embed_shows_current_vote_counts():
    import bot as bot_module

    _seed_match(bot_module.db)
    view = VoteView(bot_module.db)

    inter = _fake_interaction(_fake_member(0), _fake_guild())
    await view.vote_a.callback(inter)
    inter2 = _fake_interaction(_fake_member(1), _fake_guild())
    await view.vote_b.callback(inter2)

    embed = inter2.response.edit_message.call_args.kwargs["embed"]
    votes_field = next(f for f in embed.fields if "Votes" in f.name)
    assert "**1**" in votes_field.value  # Team A : 1
    assert "**1**" in votes_field.value  # Team B : 1


def test_build_embed_from_doc_pending():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Ascent",
        "lobby_leader_id": 0,
        "category_name": "Match #1",
        "status": "pending",
        "votes": {"0": "a", "1": "a"},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "indiquez le vainqueur" in embed.title.lower()
    votes_field = next(f for f in embed.fields if "Votes" in f.name)
    assert "**2**" in votes_field.value


def test_build_embed_from_doc_validated_a():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Ascent",
        "lobby_leader_id": 0,
        "category_name": "Match #1",
        "status": "validated_a",
        "votes": {str(i): "a" for i in range(7)},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "Team A a gagné" in embed.title


def test_build_embed_from_doc_contested():
    doc = {
        "team_a": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5)],
        "team_b": [{"id": i, "name": f"P{i}", "elo": 1500} for i in range(5, 10)],
        "map": "Ascent",
        "lobby_leader_id": 0,
        "category_name": None,
        "status": "contested",
        "votes": {},
    }
    embed = build_match_embed_from_doc(doc, "G")
    assert "admin" in embed.title.lower()


# -- Timeout --
async def test_timeout_marks_pending_match_contested():
    import bot as bot_module

    # Create an expired match (past the timeout)
    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {"$set": {"created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5)}},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    admin_role = MagicMock()
    admin_role.name = "Administrators"
    admin_role.mention = "@AdminRole"
    guild = _fake_guild(roles=[admin_role], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 1

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "contested"


async def test_timeout_self_heals_pending_with_majority_a():
    """If a `pending` match expires but already has 7+ A votes (transition
    lost due to crash / error), check_vote_timeouts must move it to
    `validated_a` instead of `contested`."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
                "votes": {str(i): "a" for i in range(MAJORITY_THRESHOLD)},
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "validated_a"
    assert match["validated_at"] is not None
    channel.send.assert_not_awaited()


async def test_timeout_self_heals_pending_with_majority_b():
    """Symmetric: 7+ B votes -> validated_b."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
                "votes": {str(i): "b" for i in range(MAJORITY_THRESHOLD)},
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "validated_b"
    channel.send.assert_not_awaited()


async def test_timeout_still_marks_contested_when_no_majority():
    """Safety net: if total >= 7 but split (e.g. 4-3), we contest."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    split_votes = {**{str(i): "a" for i in range(4)}, **{str(i): "b" for i in range(4, 7)}}
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
                "votes": split_votes,
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    admin_role = MagicMock()
    admin_role.name = "Administrators"
    admin_role.mention = "@AdminRole"
    guild = _fake_guild(roles=[admin_role], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 1

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "contested"

    channel.send.assert_awaited_once()
    args, _ = channel.send.call_args
    assert "@AdminRole" in args[0]
    assert "expiré" in args[0].lower()


async def test_timeout_does_not_affect_validated():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "status": "validated_a",
                "created_at": datetime.now(UTC) - timedelta(minutes=20),
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0
    channel.send.assert_not_awaited()


async def test_timeout_does_not_affect_recent_match():
    import bot as bot_module

    _seed_match(bot_module.db)  # cree_at = now() automatiquement

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 0


async def test_timeout_with_injectable_now():
    """Allow simulating time progression in tests."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    fake_now = datetime.now(UTC) + timedelta(minutes=VOTE_TIMEOUT_MINUTES + 1)
    flagged = await cog.check_vote_timeouts(now=fake_now)
    assert flagged == 1


async def test_timeout_falls_back_when_no_admin_role():
    """If no 'Admin' role exists: we ping `@admin` in plain text."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {"$set": {"created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5)}},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(roles=[], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    await cog.check_vote_timeouts()
    args, _ = channel.send.call_args
    assert "@admin" in args[0]


# -- Threshold const --
def test_majority_threshold_is_7():
    assert MAJORITY_THRESHOLD == 7


def test_timeout_minutes_is_90():
    assert VOTE_TIMEOUT_MINUTES == 90


async def test_vote_timeout_survives_cog_recreation():
    """Voting is entirely DB-state-driven: no in-memory cog state must be
    required for a stale vote to be correctly timed out after a bot
    restart. We simulate this by creating a match with partial votes,
    then instantiating a FRESH cog (without history) to process it."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    # Vote open for a long time with 3 A votes + 2 B votes (below the
    # majority threshold). If the bot relied only on in-memory state
    # (in-process timer, in-memory set of active matches), a restart
    # would "forget" this vote and the match would stay pending
    # indefinitely. The test locks the opposite property.
    partial_votes = {
        **{str(i): "a" for i in range(3)},
        **{str(i): "b" for i in range(3, 5)},
    }
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 1),
                "votes": partial_votes,
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    admin_role = MagicMock()
    admin_role.name = "Administrators"
    admin_role.mention = "@AdminRole"
    guild = _fake_guild(roles=[admin_role], channel=channel)

    # "Fresh" cog created after the vote expires, like after a reboot.
    fresh_cog = MatchCog(bot_module.bot, bot_module.db)
    fresh_cog.bot = MagicMock()
    fresh_cog.bot.guilds = [guild]

    flagged = await fresh_cog.check_vote_timeouts()
    assert flagged == 1

    match = repository.get_match(bot_module.db, match_id)
    assert match["status"] == "contested"
    # Partial votes from before the "reboot" are preserved.
    assert match["votes"] == partial_votes


# -- Phase 6: ELO update after validation --
def _seed_match_with_avg_2400(db, guild_id: int = 42, message_id: int = 555):
    return repository.create_match(
        db,
        origin_guild_id=guild_id,
        team_a=[{"id": i, "name": f"P{i}", "elo": 2400} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 2400} for i in range(5, 10)],
        map_name="Ascent",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=message_id,
        channel_id=100,
        queue_type="open",
    )


def _seed_db_elos(db, guild_id: int = 42, baseline: int = 2000) -> None:
    """Seed elo_col for 10 players: reflects the production situation
    where each player has at least LINK_BASE_ELO=2000 via /link-riot,
    avoiding the zero-sum floor that would neutralize winner gains."""
    col = repository.get_elo_col(db)
    for i in range(10):
        col.insert_one(
            {
                "_id": f"{i}:open",
                "name": f"P{i}",
                "elo": baseline,
                "wins": 0,
                "losses": 0,
            }
        )


async def _vote_and_verify(cog, guild, match_id, *, choice: str, db, guild_id: int = 42):
    """Helper: 7 votes for `choice` then apply ELO via _verify_match
    (henrik_client=None -> flat ELO fallback, like after 10 min without Henrik)."""
    view = cog.vote_view
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        if choice == "a":
            await view.vote_a.callback(inter)
        else:
            await view.vote_b.callback(inter)
    match_doc = repository.get_match(db, match_id)
    # force_apply=True simulates passing the Henrik timeout (flat ELO)
    await cog._verify_match(guild, match_doc, force_apply=True)


async def test_validation_triggers_elo_update_in_db():
    """After _verify_match: 5 winners +20, 5 losers -20 (flat across queues)."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(5):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        # _verify_match applies flat +20 across all queues
        assert doc["elo"] == 2020, f"Winner {i}: ELO {doc['elo']}"
        assert doc["wins"] == 1
    for i in range(5, 10):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        assert doc["elo"] == 1980  # 2000 - 20
        assert doc["losses"] == 1


async def test_validation_sends_recap_embed():
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    channel.send.assert_awaited()
    sent_embeds = [
        c.kwargs.get("embed") for c in channel.send.call_args_list if c.kwargs.get("embed")
    ]
    assert any("Team A gagne" in (e.title or "") for e in sent_embeds)
    recap = next(e for e in sent_embeds if "Team A gagne" in (e.title or ""))
    fields = {f.name: f.value for f in recap.fields}
    assert any("Vainqueurs" in n for n in fields)
    assert any("Perdants" in n for n in fields)
    assert "+20" in fields["🟢 Vainqueurs"]  # flat +20 across queues


async def test_validation_with_high_elo_match_bigger_gain():
    """Avg=3000 (Radiant) zero-sum -> gain=loss=20."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = repository.create_match(
        bot_module.db,
        origin_guild_id=42,
        team_a=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 3000} for i in range(5, 10)],
        map_name="Ascent",
        lobby_leader_id=0,
        category_name="Match #1",
        message_id=555,
        channel_id=100,
        queue_type="open",
    )
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="a", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db)
    # Flat +20 across all queues (independent of the match avg ELO).
    assert elo_col.find_one({"_id": "0:open"})["elo"] == 2020


async def test_validated_b_distributes_correctly():
    """7 votes B -> team_b wins, team_a loses (after _verify_match)."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)
    await _vote_and_verify(cog, guild, match_id, choice="b", db=bot_module.db)

    elo_col = repository.get_elo_col(bot_module.db)
    # team_b (5..9) wins +20 (flat across queues) -> 2020
    for i in range(5, 10):
        assert elo_col.find_one({"_id": f"{i}:open"})["elo"] == 2020
        assert elo_col.find_one({"_id": f"{i}:open"})["wins"] == 1
    # team_a (0..4) loses -20 -> 1980
    for i in range(5):
        assert elo_col.find_one({"_id": f"{i}:open"})["elo"] == 1980
        assert elo_col.find_one({"_id": f"{i}:open"})["losses"] == 1


async def test_vote_validation_does_not_touch_elo():
    """Safety net: the vote alone no longer touches the ELO; _verify_match is required."""
    import bot as bot_module
    from cogs.match import MatchCog

    _seed_match_with_avg_2400(bot_module.db)

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)
    cog = MatchCog(bot_module.bot, bot_module.db)

    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await cog.vote_view.vote_a.callback(inter)

    elo_col = repository.get_elo_col(bot_module.db)
    # No ELO doc created: the ELO will be applied only by _verify_match.
    for i in range(10):
        assert elo_col.find_one({"_id": str(i)}) is None


# -- Atomicity: transition_match_status (audit fix #2) --
def test_transition_match_status_succeeds_from_pending():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    res = repository.transition_match_status(
        bot_module.db,
        match_id,
        from_status="pending",
        to_status="validated_a",
    )
    assert res is not None
    assert res["status"] == "validated_a"
    assert res["validated_at"] is not None


def test_transition_match_status_fails_when_already_validated():
    """Atomicity guarantee: if another concurrent vote has already
    validated, a second transition does not succeed (returns None)."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    res = repository.transition_match_status(
        bot_module.db,
        match_id,
        from_status="pending",
        to_status="validated_b",
    )
    assert res is None


async def test_concurrent_votes_only_fire_on_validated_once():
    """Two votes voting concurrently for opposite sides at the threshold
    must trigger `on_validated` only once."""
    import bot as bot_module

    _seed_match(bot_module.db)

    fired = []

    async def on_validated(inter, match_doc):
        fired.append(match_doc.get("status"))

    view = VoteView(bot_module.db, on_validated=on_validated)
    guild = _fake_guild()

    # 6 votes 'a', 6 votes 'b' (10 players, vote-change not needed here).
    # We reach majority via 7 'a' votes first; an 8th vote then arrives
    # for 'b' while the match is already validated -> must not re-fire.
    for uid in range(7):
        inter = _fake_interaction(_fake_member(uid), guild)
        await view.vote_a.callback(inter)

    # Late vote for 'b' (the match is already validated_a)
    inter = _fake_interaction(_fake_member(7), guild)
    await view.vote_b.callback(inter)

    assert fired == ["validated_a"]


# -- ELO idempotence: claim_match_for_elo (audit fix #3) --
def test_claim_match_for_elo_succeeds_first_time():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    claim = repository.claim_match_for_elo(bot_module.db, match_id)
    assert claim is not None
    assert claim["elo_applied"] is True


def test_claim_match_for_elo_returns_none_when_already_claimed():
    """Prevent double ELO application: only the first claim passes."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    first = repository.claim_match_for_elo(bot_module.db, match_id)
    second = repository.claim_match_for_elo(bot_module.db, match_id)
    assert first is not None
    assert second is None


def test_claim_match_for_elo_rejects_non_validated_match():
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    # Status stays 'pending', no claim possible
    claim = repository.claim_match_for_elo(bot_module.db, match_id)
    assert claim is None


def test_release_elo_claim_allows_retry():
    """If ELO application raises, we release the claim to retry."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")

    repository.claim_match_for_elo(bot_module.db, match_id)
    repository.release_elo_claim(bot_module.db, match_id)
    retry = repository.claim_match_for_elo(bot_module.db, match_id)
    assert retry is not None


def test_find_validated_unverified_excludes_elo_applied():
    """A match whose ELO has already been applied must not reappear in
    the verification queue (avoid double credit)."""
    import bot as bot_module

    match_id = _seed_match(bot_module.db)
    repository.set_match_status(bot_module.db, match_id, "validated_a")
    repository.claim_match_for_elo(bot_module.db, match_id)

    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    matches = repository.find_validated_unverified(bot_module.db, cutoff)
    assert all(m["_id"] != match_id for m in matches)


# -- Category deletion after vote --
async def test_vote_validated_deletes_match_category(monkeypatch):
    """When a vote is validated, the dynamic category is deleted."""
    from unittest.mock import AsyncMock

    import bot as bot_module
    from cogs.match import _verification as match_cog_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # Seed a match with category_id=7777
    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {"$set": {"category_id": 7777, "match_number": 42}},
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    view = VoteView(bot_module.db, on_validated=cog._on_match_validated)

    for uid in range(MAJORITY_THRESHOLD):
        inter = _fake_interaction(_fake_member(uid), guild)
        await view.vote_a.callback(inter)

    delete_mock.assert_awaited_once()
    kwargs = delete_mock.await_args.kwargs
    assert kwargs["category_id"] == 7777
    assert "vote" in kwargs["reason"].lower() or "validated" in kwargs["reason"].lower()


async def test_vote_disputed_does_not_delete_category(monkeypatch):
    """When a vote is disputed (contested), the category is preserved for admin review."""
    from unittest.mock import AsyncMock

    import bot as bot_module
    from cogs.match import _verification as match_cog_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # Seed a match with category_id=8888 and expire it (triggers contested path)
    match_id = _seed_match(bot_module.db)
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "category_id": 8888,
                "created_at": datetime.now(UTC) - timedelta(minutes=VOTE_TIMEOUT_MINUTES + 5),
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    admin_role = MagicMock()
    admin_role.name = "Administrators"
    admin_role.mention = "@AdminRole"
    guild = _fake_guild(roles=[admin_role], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    cog.bot = MagicMock()
    cog.bot.guilds = [guild]

    flagged = await cog.check_vote_timeouts()
    assert flagged == 1  # contested

    # Disputed match: category must NOT be deleted
    delete_mock.assert_not_called()


# -- Phase: Henrik retry window vs flat-fallback timeout --
async def test_verify_match_within_retry_window_does_not_apply_elo(monkeypatch):
    """When Henrik returns None and the match was validated < HENRIK_VERIFY_TIMEOUT_MINUTES
    ago, _verify_match must leave the match doc untouched so the next 1-min
    tick retries. This protects the scoreboard against Henrik's typical
    10-30 min indexing delay for customs."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    # Simulate a recent validation: 6 min ago, well within the 30 min window.
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "status": "validated_a",
                "validated_at": datetime.now(UTC) - timedelta(minutes=6),
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    # Henrik returns None (custom not yet indexed).
    monkeypatch.setattr(cog, "_fetch_henrik_match_summary", AsyncMock(return_value=None))

    match_doc = repository.get_match(bot_module.db, match_id)
    await cog._verify_match(guild, match_doc)

    # Match must NOT have been claimed nor verified — next tick should retry.
    fresh = repository.get_match(bot_module.db, match_id)
    assert fresh.get("elo_applied") is not True, "elo_applied set within retry window"
    assert fresh.get("henrik_verified") is not True, "henrik_verified set within retry window"

    # ELO untouched.
    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(10):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        assert doc["elo"] == 2000, f"ELO mutated for player {i}"


async def test_verify_match_after_timeout_applies_flat_elo(monkeypatch):
    """When Henrik never responded and HENRIK_VERIFY_TIMEOUT_MINUTES has elapsed,
    _verify_match must apply flat ELO and mark henrik_verified=True with
    found=False (no scoreboard, but ELO is no longer stuck in limbo)."""
    import bot as bot_module
    from cogs.match import MatchCog

    match_id = _seed_match_with_avg_2400(bot_module.db)
    _seed_db_elos(bot_module.db)

    # Simulate a validation that has passed the timeout.
    bot_module.db["matches"].update_one(
        {"_id": match_id},
        {
            "$set": {
                "status": "validated_a",
                "validated_at": datetime.now(UTC)
                - timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES + 1),
            }
        },
    )

    channel = MagicMock()
    channel.send = AsyncMock()
    guild = _fake_guild(channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db)
    monkeypatch.setattr(cog, "_fetch_henrik_match_summary", AsyncMock(return_value=None))

    match_doc = repository.get_match(bot_module.db, match_id)
    await cog._verify_match(guild, match_doc)

    fresh = repository.get_match(bot_module.db, match_id)
    assert fresh.get("elo_applied") is True
    assert fresh.get("henrik_verified") is True
    assert fresh.get("henrik_found") is False, "found=False when Henrik never replied"

    # Flat ELO applied.
    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(5):
        assert elo_col.find_one({"_id": f"{i}:open"})["elo"] == 2020
    for i in range(5, 10):
        assert elo_col.find_one({"_id": f"{i}:open"})["elo"] == 1980
