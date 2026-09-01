"""Integration tests for the match cog (formation + persistence + reset queue)."""

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.match import VOTE_A_BTN_ID, VOTE_B_BTN_ID, MatchCog, VoteView, build_match_embed
from services import elo_calc, repository
from services.team_balancer import Player


def _fake_member(member_id: int, name: str = "User", voice_channel=None):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.guild = MagicMock(roles=[])
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.move_to = AsyncMock()
    if voice_channel is not None:
        voice = MagicMock()
        voice.channel = voice_channel
        m.voice = voice
    else:
        m.voice = None
    return m


def _fake_category(
    name: str,
    t1_empty: bool = True,
    t2_empty: bool = True,
    with_prep: bool = True,
    with_waiting: bool = True,
):
    cat = MagicMock()
    cat.name = name
    t1 = MagicMock()
    t1.name = "Team 1"
    t1.members = [] if t1_empty else [object()]
    t2 = MagicMock()
    t2.name = "Team 2"
    t2.members = [] if t2_empty else [object()]
    vcs = [t1, t2]
    if with_waiting:
        waiting = MagicMock()
        waiting.name = "Waiting Match"
        waiting.id = 800 + (hash(name) % 100)
        waiting.members = []
        vcs.append(waiting)
    cat.voice_channels = vcs
    if with_prep:
        prep = MagicMock()
        prep.name = "match-preparation"
        prep.id = 700 + (hash(name) % 100)
        prep.send = AsyncMock(return_value=MagicMock(id=555))
        prep.category = cat  # Back-reference to parent category
        cat.text_channels = [prep]
    else:
        cat.text_channels = []
    return cat


def _fake_channel(channel_id: int = 100):
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock(return_value=MagicMock(id=555, channel=ch))
    return ch


def _fake_guild(guild_id: int = 42, members=None, categories=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.categories = categories or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == int(mid)), None)
    g.get_channel = lambda cid: channel
    return g


def _fake_interaction(guild, user=None):
    inter = MagicMock()
    inter.guild = guild
    inter.user = user or _fake_member(1)
    inter.guild_id = guild.id
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _seed_full_queue(
    db,
    guild_id: int,
    channel_id: int = 100,
    queue_type: str = "open",
):
    """Create the active queue + 10 linked Riot accounts + their server ELO.

    By default we simulate an Open Queue. Pro Queue tests pass
    `queue_type="open"` explicitly.
    """
    repository.setup_active_queue(
        db,
        guild_id=guild_id,
        queue_type=queue_type,
        channel_id=channel_id,
        message_id=999,
    )
    elo_col = repository.get_elo_col(db)
    for i in range(10):
        repository.link_riot_account(
            db,
            user_id=i,
            riot_name=f"P{i}",
            riot_tag="EUW",
            riot_region="eu",
            puuid=f"pu{i}",
            peak_elo=1500 + i * 50,
            source="peak_recent",
        )
        # Compound _id `<uid>:<queue_type>` for the player doc.
        elo_col.insert_one(
            {
                "_id": repository.player_doc_id(i, queue_type),
                "name": f"P{i}",
                "elo": 1500 + i * 50,
                "wins": 0,
                "losses": 0,
                "linked_once": True,
            }
        )
        repository.add_player_to_queue(
            db,
            guild_id=guild_id,
            queue_type=queue_type,
            user_id=i,
        )
    return repository.get_active_queue(
        db,
        guild_id=guild_id,
        queue_type=queue_type,
    )


def _make_fake_channels(cat_id: int = 4242, cat_name: str = "Match #1", prep_id: int = 999):
    """Build a fully-wired MatchChannels mock for on_queue_full tests."""
    from services.match_category import MatchChannels

    fake_category = MagicMock()
    fake_category.id = cat_id
    fake_category.name = cat_name

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = prep_id
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = prep_id + 10
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = prep_id + 11
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = prep_id + 12
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    return MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )


# ── on_queue_full : succes ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_queue_full_posts_message_with_view(monkeypatch):
    """on_queue_full sends match embed + VoteView to the prep channel."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is not None
    fake_channels.prep_channel.send.assert_awaited_once()
    _, kwargs = fake_channels.prep_channel.send.call_args
    assert "Match trouvé" in kwargs["content"]
    embed = kwargs["embed"]
    assert "Carte" in embed.description
    assert any("Team A" in f.name for f in embed.fields)
    assert any("Team B" in f.name for f in embed.fields)
    assert isinstance(kwargs["view"], VoteView)


@pytest.mark.asyncio
async def test_on_queue_full_persists_match(monkeypatch):
    """on_queue_full stores a match doc with expected fields."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels(cat_id=1111, cat_name="Match #1", prep_id=777)
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["status"] == "pending"
    assert match["map"] in elo_calc.MAPS
    assert match["category_name"] == "Match #1"
    assert match["message_id"] == 555
    assert match["channel_id"] == 777
    assert len(match["team_a"]) == 5
    assert len(match["team_b"]) == 5
    assert match["votes"] == {}
    assert int(match["lobby_leader_id"]) in range(10)


@pytest.mark.asyncio
async def test_on_queue_full_resets_queue(monkeypatch):
    """After a successful match formation the active queue is cleared."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None


@pytest.mark.asyncio
async def test_on_queue_full_strips_buttons_from_old_queue_message(monkeypatch):
    """After match formation the stale queue message has its Join/Leave
    buttons removed (view=None) so it can no longer mutate the new queue."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    # _seed_full_queue registers the queue message id as 999.
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    old_msg = MagicMock()
    old_msg.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old_msg)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is not None
    channel.fetch_message.assert_awaited_once_with(999)
    old_msg.edit.assert_awaited_once_with(view=None)


@pytest.mark.asyncio
async def test_on_queue_full_balanced_teams_in_persistence(monkeypatch):
    """The persisted teams must be ELO-balanced within acceptable range."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, match_id)
    sum_a = sum(p["elo"] for p in match["team_a"])
    sum_b = sum(p["elo"] for p in match["team_b"])
    diff = abs(sum_a - sum_b)
    assert diff <= 100, f"diff={diff}, expected <=100 on this set"


@pytest.mark.asyncio
async def test_on_queue_full_aborts_if_player_unlinked(monkeypatch):
    """If a player has no linked Riot account, the match is aborted."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    repository.unlink_riot_account(bot_module.db, user_id=5)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    result = await cog.on_queue_full(inter, queue_doc, "open")

    assert result is None
    # Queue is deleted on failure
    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None
    # Verify the user-facing cancel message was sent
    channel.send.assert_awaited()
    send_args = channel.send.await_args
    sent_content = send_args.args[0] if send_args.args else send_args.kwargs.get("content", "")
    assert "annulé" in sent_content.lower()


# -- VoteView stub (Phase 4 - implemented in Phase 5) --
async def test_vote_view_buttons_have_stable_custom_ids():
    import bot as bot_module

    view = VoteView(bot_module.db)
    # Look for the custom_ids on the children
    custom_ids = {c.custom_id for c in view.children}
    assert VOTE_A_BTN_ID in custom_ids
    assert VOTE_B_BTN_ID in custom_ids


# -- Order overwrites -> message (audit user) --
@pytest.mark.asyncio
async def test_overwrites_set_before_match_message_sent(monkeypatch):
    """create_match_category (which sets channel overwrites) must be awaited
    BEFORE prep_channel.send - otherwise players can't see the announce."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    events = []
    fake_channels = _make_fake_channels()

    async def _fake_create(**kwargs):
        events.append("create_match_category")
        return fake_channels

    async def _prep_send(*args, **kwargs):
        events.append("prep_send")
        msg = MagicMock()
        msg.id = 555
        return msg

    fake_channels.prep_channel.send.side_effect = _prep_send

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", _fake_create)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert "create_match_category" in events, "create_match_category must be called"
    assert "prep_send" in events, "prep_channel.send must be called"
    assert events.index("create_match_category") < events.index("prep_send"), (
        "create_match_category must complete before prep_channel.send"
    )


@pytest.mark.asyncio
async def test_players_moved_to_team_vcs(monkeypatch):
    """In open queue, players in a voice channel are moved to Team 1 or Team 2 (5+5)."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    waiting_room = MagicMock()
    waiting_room.name = "Waiting Room"
    waiting_room.id = 999

    fake_channels = _make_fake_channels(cat_name="Match #1")
    team1 = fake_channels.team1_vc
    team2 = fake_channels.team2_vc
    # The cog looks up the category by name from guild.categories, so include it.
    guild_category = fake_channels.category

    members = [_fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[guild_category], channel=channel)
    inter = _fake_interaction(guild)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    dests = []
    for m in members:
        if m.move_to.await_count:
            dest = m.move_to.await_args.args[0]
            assert dest in (team1, team2), f"Player {m.id} moved to unexpected VC"
            dests.append(dest)
    assert len(dests) == 10
    assert dests.count(team1) == 5
    assert dests.count(team2) == 5


@pytest.mark.asyncio
async def test_player_already_in_team_vc_not_moved(monkeypatch):
    """Players already in their team VC are not re-moved to it."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    fake_channels = _make_fake_channels(cat_name="Match #1")
    team1 = fake_channels.team1_vc
    guild_category = fake_channels.category

    # All players start in Team 1 VC
    members = [_fake_member(i, f"P{i}", voice_channel=team1) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[guild_category], channel=channel)
    inter = _fake_interaction(guild)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    # Nobody should be moved back to Team 1 (they are already there)
    for m in members:
        for call in m.move_to.await_args_list:
            assert call.args[0] is not team1, (
                "Player already in Team 1 should not be re-moved there"
            )


@pytest.mark.asyncio
async def test_queue_full_does_not_crash_when_no_team_vcs(monkeypatch):
    """If the category has no voice channels, the match is still created (graceful fallback)."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module
    from services.match_category import MatchChannels

    fake_category = MagicMock()
    fake_category.id = 7777
    fake_category.name = "Match #1"
    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 777
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=42424242))
    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = []

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=None,
        team2_vc=None,
        waiting_match_vc=None,
    )

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")
    assert match_id is not None


# ── Draft + ban stubs for open/advanced tests ───────────────────────────────


def _patch_draft_and_ban_for_pro(monkeypatch, fake_plan):
    """Make on_queue_full's open/advanced branch complete instantly with fake_plan.

    Stubs out CaptainDraftSession, MapBanSession, pick_captains, and
    build_plan_from_draft so tests asserting downstream behavior (DB
    persistence, embed posting) can use queue_type="open" without
    hanging on the draft/ban awaits.
    """

    class _FakeDraftSession:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            return MagicMock(
                cap_a=MagicMock(id=1),
                cap_b=MagicMock(id=2),
                team_a=tuple(MagicMock(id=i) for i in range(1, 6)),
                team_b=tuple(MagicMock(id=i) for i in range(6, 11)),
            )

    class _FakeBanSession:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            return MagicMock(selected_map="Haven", ban_history=())

    monkeypatch.setattr("cogs.match._formation.CaptainDraftSession", _FakeDraftSession)
    monkeypatch.setattr("cogs.match._formation.MapBanSession", _FakeBanSession)
    monkeypatch.setattr(
        "cogs.match._formation.pick_captains",
        lambda players, *, rng: (
            (players[0], players[1]) if len(players) >= 2 else (MagicMock(id=1), MagicMock(id=2))
        ),
    )
    monkeypatch.setattr(
        "cogs.match._formation.build_plan_from_draft",
        lambda *a, **kw: fake_plan,
    )


# ── queue_type propagation ────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_queue_full_persists_queue_type_in_match_doc(monkeypatch):
    """The match doc must store the queue_type from the queue."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module
    from services.match_service import MatchPlan
    from services.team_balancer import Player, balance_teams

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="open")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    # Build a fake_plan whose shape matches what build_plan_from_draft would return
    # so the downstream persistence + embed code can proceed without hanging.
    _players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    _teams = balance_teams(_players)
    fake_plan = MatchPlan(
        teams=_teams,
        map_name="Haven",
        lobby_leader=_players[0],
        category_name="Match #1",
    )
    _patch_draft_and_ban_for_pro(monkeypatch, fake_plan)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    monkeypatch.setattr(cog, "_move_players_to_waiting_match", AsyncMock())
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["queue_type"] == "open"


@pytest.mark.asyncio
async def test_on_queue_full_passes_queue_type_to_create_match(monkeypatch):
    """Spy on repository.create_preparing_match to verify queue_type
    kwarg is forwarded at the placeholder-insert stage."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="open")

    captured: dict = {}
    real_create_preparing = repository.create_preparing_match

    def spy_create_preparing(*args, **kwargs):
        captured.update(kwargs)
        return real_create_preparing(*args, **kwargs)

    monkeypatch.setattr("services.repository.create_preparing_match", spy_create_preparing)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert captured.get("queue_type") == "open"


# -- Task 7: dynamic category creation ----------------------------------
@pytest.mark.asyncio
async def test_start_match_creates_dynamic_category(monkeypatch):
    """After Task 7 wire-up: start_match must reserve a number + create
    the category via services.match_category, and persist category_id +
    match_number on the match doc."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module

    fake_category = MagicMock()
    fake_category.id = 4242
    fake_category.name = "Match #43"

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 999
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 991
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 992
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 993
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )

    mock_create = AsyncMock(return_value=fake_channels)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 43)
    monkeypatch.setattr(match_cog_module, "create_match_category", mock_create)

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild, user=members[0])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is not None, "on_queue_full must return a match_id"
    mock_create.assert_awaited_once()

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["category_id"] == 4242
    assert match["category_name"] == "Match #43"
    assert match["match_number"] == 43


# -- build_match_embed --
def test_build_match_embed_shows_all_players_and_map():
    from services.match_service import MatchPlan
    from services.team_balancer import balance_teams

    players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    teams = balance_teams(players)
    plan = MatchPlan(
        teams=teams, map_name="Ascent", lobby_leader=players[0], category_name="Match #1"
    )

    embed = build_match_embed(plan, "MyGuild")
    assert "Ascent" in embed.description
    assert "<@0>" in embed.description  # leader
    fields_str = " ".join(f.value for f in embed.fields)
    for i in range(10):
        assert f"<@{i}>" in fields_str
    assert "Match #1" in fields_str


# -- match_cancel: category deletion --
@pytest.mark.asyncio
async def test_admin_cancel_deletes_match_category(monkeypatch):
    """When admin runs /match-cancel, the dynamic category is deleted."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    import cogs.match._formation as match_formation_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # Seed a pending match with category_id=5555 so cancel_match_atomically finds it.
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    guild.roles = []
    inter_start = _fake_interaction(guild, user=members[0])

    # Create the match via on_queue_full (patching create_match_category to avoid Discord calls).
    fake_category = MagicMock()
    fake_category.id = 5555
    fake_category.name = "Match #1"
    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 999
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))
    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 991
    fake_team1.members = []
    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 992
    fake_team2.members = []
    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 993
    fake_waiting.members = []
    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )
    monkeypatch.setattr(match_formation_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_formation_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    await cog.on_queue_full(inter_start, queue_doc, "open")

    # The match is stored with channel_id = prep_channel.id (999) - use that.
    inter_cancel = _fake_interaction(guild)
    inter_cancel.channel_id = fake_prep.id  # 999
    inter_cancel.channel = channel
    inter_cancel.response = MagicMock()
    inter_cancel.response.defer = AsyncMock()

    await cog.match_cancel.callback(cog, inter_cancel)

    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 5555


# ── Branch coverage: open vs open/advanced ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_queue_full_open_queue_calls_plan_match_not_draft(monkeypatch):
    """Open queue: plan_match is called; CaptainDraftSession is NOT constructed."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module
    from services.match_service import MatchPlan
    from services.team_balancer import Player, balance_teams

    calls = {"plan_match": 0, "draft_ctor": 0, "ban_ctor": 0}

    _players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    _teams = balance_teams(_players)
    fake_plan = MatchPlan(
        teams=_teams,
        map_name="Ascent",
        lobby_leader=_players[0],
        category_name="Match #1",
    )

    def fake_plan_match(*a, **kw):
        calls["plan_match"] += 1
        return fake_plan

    class _NeverDraft:
        def __init__(self, *a, **kw):
            calls["draft_ctor"] += 1
            raise AssertionError("CaptainDraftSession must NOT be constructed for open queue")

        async def run(self):
            raise AssertionError("never")

    class _NeverBan:
        def __init__(self, *a, **kw):
            calls["ban_ctor"] += 1
            raise AssertionError("MapBanSession must NOT be constructed for open queue")

        async def run(self):
            raise AssertionError("never")

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )
    monkeypatch.setattr("cogs.match._formation.plan_match", fake_plan_match)
    monkeypatch.setattr("cogs.match._formation.CaptainDraftSession", _NeverDraft)
    monkeypatch.setattr("cogs.match._formation.MapBanSession", _NeverBan)

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="open")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert calls["plan_match"] == 1
    assert calls["draft_ctor"] == 0
    assert calls["ban_ctor"] == 0


@pytest.mark.asyncio
async def test_on_queue_full_pro_queue_runs_draft_then_ban_then_build(monkeypatch):
    """Pro queue: draft -> ban -> build_plan_from_draft with map_name from ban."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module
    from services.match_service import MatchPlan
    from services.team_balancer import Player, balance_teams

    seq: list[str] = []

    _players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    _teams = balance_teams(_players)

    class _FakeDraft:
        def __init__(self, *a, **kw):
            seq.append("draft_ctor")

        async def run(self):
            seq.append("draft_run")
            return MagicMock(
                cap_a=MagicMock(id=1),
                cap_b=MagicMock(id=2),
                team_a=tuple(MagicMock(id=i) for i in range(1, 6)),
                team_b=tuple(MagicMock(id=i) for i in range(6, 11)),
            )

    class _FakeBan:
        def __init__(self, *a, **kw):
            seq.append("ban_ctor")

        async def run(self):
            seq.append("ban_run")
            return MagicMock(selected_map="Haven", ban_history=())

    def fake_build_plan(*a, **kw):
        seq.append(f"build({kw.get('map_name')})")
        return MatchPlan(
            teams=_teams,
            map_name=kw.get("map_name", "Haven"),
            lobby_leader=_players[0],
            category_name="Match #1",
        )

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )
    monkeypatch.setattr("cogs.match._formation.CaptainDraftSession", _FakeDraft)
    monkeypatch.setattr("cogs.match._formation.MapBanSession", _FakeBan)
    monkeypatch.setattr("cogs.match._formation.build_plan_from_draft", fake_build_plan)
    monkeypatch.setattr(
        "cogs.match._formation.pick_captains",
        lambda players, *, rng: (players[0], players[1]),
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="advanced")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    monkeypatch.setattr(cog, "_move_players_to_waiting_match", AsyncMock())
    await cog.on_queue_full(inter, queue_doc, "advanced")

    assert seq == ["draft_ctor", "draft_run", "ban_ctor", "ban_run", "build(Haven)"]


@pytest.mark.asyncio
async def test_on_queue_full_semipro_queue_runs_draft_then_ban(monkeypatch):
    """Semi-Pro queue: same branching as Pro (verifies condition is 'in (open, advanced)' not '== open')."""
    import bot as bot_module
    import cogs.match._formation as match_cog_module
    from services.match_service import MatchPlan
    from services.team_balancer import Player, balance_teams

    seq: list[str] = []

    _players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    _teams = balance_teams(_players)

    class _FakeDraft:
        def __init__(self, *a, **kw):
            seq.append("draft_ctor")

        async def run(self):
            seq.append("draft_run")
            return MagicMock(
                cap_a=MagicMock(id=1),
                cap_b=MagicMock(id=2),
                team_a=tuple(MagicMock(id=i) for i in range(1, 6)),
                team_b=tuple(MagicMock(id=i) for i in range(6, 11)),
            )

    class _FakeBan:
        def __init__(self, *a, **kw):
            seq.append("ban_ctor")

        async def run(self):
            seq.append("ban_run")
            return MagicMock(selected_map="Haven", ban_history=())

    def fake_build_plan(*a, **kw):
        seq.append(f"build({kw.get('map_name')})")
        return MatchPlan(
            teams=_teams,
            map_name=kw.get("map_name", "Haven"),
            lobby_leader=_players[0],
            category_name="Match #1",
        )

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )
    monkeypatch.setattr("cogs.match._formation.CaptainDraftSession", _FakeDraft)
    monkeypatch.setattr("cogs.match._formation.MapBanSession", _FakeBan)
    monkeypatch.setattr("cogs.match._formation.build_plan_from_draft", fake_build_plan)
    monkeypatch.setattr(
        "cogs.match._formation.pick_captains",
        lambda players, *, rng: (players[0], players[1]),
    )

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="advanced")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    monkeypatch.setattr(cog, "_move_players_to_waiting_match", AsyncMock())
    await cog.on_queue_full(inter, queue_doc, "advanced")

    assert seq == ["draft_ctor", "draft_run", "ban_ctor", "ban_run", "build(Haven)"]


@pytest.mark.asyncio
async def test_persist_extended_stats_inserts_and_updates_aggregate(monkeypatch):
    """_persist_extended_stats must insert per-match docs and update
    the aggregate counters when the insert is fresh.
    """
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    from services.match_verifier import PlayerStatsExtended

    extended = (
        PlayerStatsExtended(
            user_id="111",
            puuid="P-A",
            queue_type="open",
            map_name="Ascent",
            agent="Jett",
            team="Red",
            win=True,
            rounds_played=24,
            acs=225.0,
            kills=22,
            deaths=14,
            assists=5,
            damage_made=4123,
            damage_received=3580,
            headshots=18,
            bodyshots=50,
            legshots=4,
            multikills_2k=3,
            multikills_3k=1,
            multikills_4k=0,
            multikills_5k=0,
            first_kills=4,
            first_deaths=2,
            kast_rounds=19,
            rating_2_0=1.34,
        ),
    )

    insert_calls: list = []
    update_calls: list = []
    monkeypatch.setattr(
        match_cog_module.repository,
        "insert_match_player_stats",
        lambda db, docs: insert_calls.append(list(docs)) or len(docs),
    )
    monkeypatch.setattr(
        match_cog_module.repository,
        "update_rating_aggregates",
        lambda db, deltas: update_calls.append(list(deltas)),
    )

    cog = match_cog_module.MatchCog(bot_module.bot, bot_module.db)
    await cog._persist_extended_stats(
        match_id="match-1",
        extended=extended,
    )

    assert len(insert_calls) == 1 and len(insert_calls[0]) == 1
    doc = insert_calls[0][0]
    assert doc["_id"] == "match-1:111"
    assert doc["rating_2_0"] == 1.34
    assert doc["match_id"] == "match-1"
    assert doc["agent"] == "Jett"
    assert doc["map"] == "Ascent"

    assert len(update_calls) == 1 and len(update_calls[0]) == 1
    delta = update_calls[0][0]
    assert delta["user_id"] == "111"
    assert delta["queue_type"] == "open"
    assert delta["games"] == 1
    assert delta["kills"] == 22
    assert delta["rating_2_0_sum"] == 1.34


@pytest.mark.asyncio
async def test_persist_extended_stats_skips_aggregate_on_duplicate_insert(monkeypatch):
    """Duplicate insert (idempotent retry) -> skip the aggregate
    update to prevent double-counting.
    """
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    from services.match_verifier import PlayerStatsExtended

    one = (
        PlayerStatsExtended(
            user_id="111",
            puuid="P-A",
            queue_type="open",
            map_name="Ascent",
            agent="Jett",
            team="Red",
            win=True,
            rounds_played=24,
            acs=200.0,
            kills=0,
            deaths=0,
            assists=0,
            damage_made=0,
            damage_received=0,
            headshots=0,
            bodyshots=0,
            legshots=0,
            multikills_2k=0,
            multikills_3k=0,
            multikills_4k=0,
            multikills_5k=0,
            first_kills=0,
            first_deaths=0,
            kast_rounds=0,
            rating_2_0=0.0,
        ),
    )

    monkeypatch.setattr(
        match_cog_module.repository,
        "insert_match_player_stats",
        lambda db, docs: 0,
    )
    update_called: list = []
    monkeypatch.setattr(
        match_cog_module.repository,
        "update_rating_aggregates",
        lambda db, deltas: update_called.append(deltas),
    )

    cog = match_cog_module.MatchCog(bot_module.bot, bot_module.db)
    await cog._persist_extended_stats(
        match_id="match-x",
        extended=one,
    )

    assert update_called == []


@pytest.mark.asyncio
async def test_persist_extended_stats_swallows_insert_exception(monkeypatch):
    """Mongo error on insert -> log + skip aggregate, never raise."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    from services.match_verifier import PlayerStatsExtended

    one = (
        PlayerStatsExtended(
            user_id="111",
            puuid="P-A",
            queue_type="open",
            map_name="Ascent",
            agent="Jett",
            team="Red",
            win=True,
            rounds_played=24,
            acs=200.0,
            kills=0,
            deaths=0,
            assists=0,
            damage_made=0,
            damage_received=0,
            headshots=0,
            bodyshots=0,
            legshots=0,
            multikills_2k=0,
            multikills_3k=0,
            multikills_4k=0,
            multikills_5k=0,
            first_kills=0,
            first_deaths=0,
            kast_rounds=0,
            rating_2_0=0.0,
        ),
    )

    def boom(db, docs):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(
        match_cog_module.repository,
        "insert_match_player_stats",
        boom,
    )
    update_called: list = []
    monkeypatch.setattr(
        match_cog_module.repository,
        "update_rating_aggregates",
        lambda db, deltas: update_called.append(deltas),
    )

    cog = match_cog_module.MatchCog(bot_module.bot, bot_module.db)
    # Must NOT raise.
    await cog._persist_extended_stats(
        match_id="match-x",
        extended=one,
    )
    assert update_called == []
