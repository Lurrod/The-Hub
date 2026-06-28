"""
Integration tests for SLASH commands and buttons (LeaderboardView).

dpytest does not fully support slash commands or components. We use
direct Discord mocks instead: we build a fake discord.Interaction, call
the command callback, and assert on the calls to the Discord methods.

To run:
    pytest tests/test_bot_slash.py -v
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

# After refactor: the slash commands live in cogs/elo_admin.py and
# cogs/admin.py. bot.add_cog is not called in tests (no setup_hook), so
# we instantiate the cog manually and re-expose its commands on the
# `bot` module to preserve the old test call sites
# (`bot_module.win.callback(inter, ...)`). The `callback` signature of
# a command in a cog includes `self`, so the calls must pass the cog
# instance as the 1st argument.
import bot as _bot_module
from cogs.admin import AdminCog
from cogs.elo_admin import ELOAdminCog

_elo_cog = ELOAdminCog(_bot_module.bot, _bot_module.db)
_admin_cog = AdminCog(_bot_module.bot, _bot_module.db)


# Re-expose the cog commands on the `bot` module (with auto-bind of
# `self` via a closure) to avoid touching 13 tests.
class _BoundCommand:
    def __init__(self, cog, cmd):
        self._cog = cog
        self._cmd = cmd

    @property
    def callback(self):
        # Wrap so `.callback(inter, ...)` automatically binds self=cog.
        async def _bound(*args, **kwargs):
            return await self._cmd.callback(self._cog, *args, **kwargs)

        return _bound


for _name in (
    "win",
    "lose",
    "leaderboard",
    "resetelo",
    "reset_queue",
    "elomodify",
    "winmodify",
    "losemodify",
):
    setattr(_bot_module, _name, _BoundCommand(_elo_cog, getattr(_elo_cog, _name)))
for _name in ("map_pick", "coinflip", "clear", "help_cmd", "setup_bot", "bypass"):
    setattr(_bot_module, _name, _BoundCommand(_admin_cog, getattr(_admin_cog, _name)))


def _fake_member(member_id: int, name: str, *, manage_guild: bool = False):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.guild_permissions.manage_guild = manage_guild
    m.roles = []
    avatar = MagicMock()
    avatar.url = f"https://cdn.discordapp.com/embed/avatars/{member_id % 6}.png"
    avatar.replace.return_value = avatar
    m.display_avatar = avatar
    return m


def _fake_guild(guild_id: int, name: str = "TestGuild", members=None):
    g = MagicMock()
    g.id = guild_id
    g.name = name
    g.members = members or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == mid), None)
    return g


def _fake_interaction(user, guild):
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.followup.edit_message = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.message = MagicMock()
    inter.message.id = 999
    return inter


# ── /stats ────────────────────────────────────────────────────────
async def test_slash_stats_unknown_player():
    import bot as bot_module
    from cogs.stats._cog import StatsCog

    user = _fake_member(1, "Alice")
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    cog = StatsCog(bot_module.bot, bot_module.db)
    await cog.stats.callback(cog, inter, "open", user)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "n'a pas encore joué" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_slash_stats_known_player():
    import bot as bot_module
    from cogs.stats._cog import StatsCog

    user = _fake_member(1, "Alice")
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": "1:open",
            "user_id": "1",
            "queue_type": "open",
            "name": "Alice",
            "elo": 200,
            "wins": 8,
            "losses": 2,
        }
    )

    cog = StatsCog(bot_module.bot, bot_module.db)
    await cog.stats.callback(cog, inter, "open", user)

    inter.response.send_message.assert_awaited_once()
    embed = inter.response.send_message.call_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "200" in fields["🏅 ELO"]
    assert "80" in fields["📈 Winrate"]  # 80%


# ── /win ──────────────────────────────────────────────────────────
async def test_slash_win_no_permission():
    import bot as bot_module

    user = _fake_member(1, "Alice", manage_guild=False)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[user, target])
    inter = _fake_interaction(user, guild)

    await bot_module.win.callback(inter, queue="open", player1=target)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "n'avez pas la permission" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_slash_win_5_players_distributes_elo_v2():
    """Position weighting: player1->player5 = +20, +18, +17, +16, +15."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(10 + i, f"P{i}") for i in range(5)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    await bot_module.win.callback(
        inter,
        queue="open",
        player1=targets[0],
        player2=targets[1],
        player3=targets[2],
        player4=targets[3],
        player5=targets[4],
    )

    expected_gains = bot_module.WIN_DELTAS_BY_SLOT  # (20, 18, 17, 16, 15)
    col = bot_module.get_elo_col()
    for slot, t in enumerate(targets):
        doc = col.find_one({"_id": f"{t.id}:open"})
        gain = expected_gains[slot]
        expected_elo = 2000 + gain
        assert doc["elo"] == expected_elo, (
            f"{t.display_name}: expected {expected_elo}, got {doc['elo']}"
        )
        assert doc["wins"] == 1


async def test_slash_win_base_is_constant_regardless_of_avg():
    """Per-position gain stays constant whatever the match avg ELO."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(20 + i, f"R{i}") for i in range(2)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    # Seed a server ELO of 3000 (Radiant): position-weighted gains are independent of the avg.
    col = bot_module.get_elo_col()
    for t in targets:
        col.insert_one(
            {
                "_id": f"{t.id}:open",
                "user_id": str(t.id),
                "queue_type": "open",
                "name": t.display_name,
                "elo": 3000,
                "wins": 0,
                "losses": 0,
            }
        )

    await bot_module.win.callback(inter, queue="open", player1=targets[0], player2=targets[1])

    expected_gains = bot_module.WIN_DELTAS_BY_SLOT  # slot 0: +20, slot 1: +18
    for slot, t in enumerate(targets):
        doc = col.find_one({"_id": f"{t.id}:open"})
        expected = 3000 + expected_gains[slot]
        assert doc["elo"] == expected, f"{t.display_name}: expected {expected}, got {doc['elo']}"


# ── /lose ─────────────────────────────────────────────────────────
async def test_slash_lose_floors_at_zero():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    partner = _fake_member(3, "Boost")
    guild = _fake_guild(42, members=[admin, target, partner])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": "2:open",
            "user_id": "2",
            "queue_type": "open",
            "name": "Bob",
            "elo": 5,
            "wins": 0,
            "losses": 0,
        }
    )
    col.insert_one(
        {
            "_id": "3:open",
            "user_id": "3",
            "queue_type": "open",
            "name": "Boost",
            "elo": 2995,
            "wins": 0,
            "losses": 0,
        }
    )

    # /lose weighted by position: slot 0 -> -10, slot 1 -> -10
    # Bob (slot 0)  : max(0, 5 - 10)    = 0
    # Boost (slot 1): 2995 - 10         = 2985
    await bot_module.lose.callback(inter, queue="open", player1=target, player2=partner)

    losses = bot_module.LOSE_DELTAS_BY_SLOT
    assert col.find_one({"_id": "2:open"})["elo"] == max(0, 5 - losses[0])
    assert col.find_one({"_id": "3:open"})["elo"] == 2995 - losses[1]


# -- /leaderboard + LeaderboardView (the initial bug) --
async def test_slash_leaderboard_creates_view_with_pagination():
    """
    Key case: 30 players -> 2 pages.
    Verifies the command sends both a file AND a view with 3 buttons.
    """
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    members = [_fake_member(100 + i, f"User{i}") for i in range(30)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    for i, m in enumerate(members):
        col.insert_one(
            {
                "_id": f"{m.id}:open",
                "user_id": str(m.id),
                "queue_type": "open",
                "name": m.display_name,
                "elo": 100 + i,
                "wins": i,
                "losses": 0,
                "last_played": datetime.now(UTC),
            }
        )

    await bot_module.leaderboard.callback(inter, queue="open")

    # The 1st page goes through interaction.followup.send (after defer)
    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_awaited_once()

    kwargs = inter.followup.send.call_args.kwargs
    assert "file" in kwargs, "No file sent"
    assert "view" in kwargs, "No view sent"

    view = kwargs["view"]
    assert view.page == 0
    # 3 buttons: prev, page_btn (label), next
    assert len(view.children) == 3
    # prev disabled on page 0, next active
    assert view.children[0].disabled is True
    assert view.children[2].disabled is False


async def test_slash_leaderboard_next_button_navigates_to_page_2():
    """
    THE BUG TEST: we simulate a click on the next button and verify the
    page changes and a new file is sent.
    """
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    members = [_fake_member(100 + i, f"User{i}") for i in range(30)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    for i, m in enumerate(members):
        col.insert_one(
            {
                "_id": f"{m.id}:open",
                "user_id": str(m.id),
                "queue_type": "open",
                "name": m.display_name,
                "elo": 100 + i,
                "wins": 0,
                "losses": 0,
                "last_played": datetime.now(UTC),
            }
        )

    # 1) Run the command to get the view
    await bot_module.leaderboard.callback(inter, queue="open")
    view = inter.followup.send.call_args.kwargs["view"]
    assert view.page == 0

    # 2) Simulate a "next" click: directly call the _go helper
    btn_inter = _fake_interaction(admin, guild)
    await view._go(btn_inter, view.page + 1)

    # 3) Verify: page = 1, defer + edit called
    assert view.page == 1, f"Page did not change: {view.page}"
    btn_inter.response.defer.assert_awaited_once()
    btn_inter.followup.edit_message.assert_awaited_once()

    edit_kwargs = btn_inter.followup.edit_message.call_args.kwargs
    assert edit_kwargs["message_id"] == btn_inter.message.id
    assert "attachments" in edit_kwargs and len(edit_kwargs["attachments"]) == 1
    assert "view" in edit_kwargs

    # 4) Buttons updated: on page 1 (=last), next disabled, prev active
    assert view.children[0].disabled is False  # prev
    assert view.children[2].disabled is True  # next


async def test_slash_leaderboard_clicking_next_past_last_page_is_noop():
    """Bounds guard: going past the last page does not break anything."""
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    # 16 players -> 2 pages (page 0 and page 1)
    members = [_fake_member(100 + i, f"User{i}") for i in range(16)]
    guild = _fake_guild(42, members=[admin] + members)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    for i, m in enumerate(members):
        col.insert_one(
            {
                "_id": f"{m.id}:open",
                "user_id": str(m.id),
                "queue_type": "open",
                "name": m.display_name,
                "elo": 100 + i,
                "wins": 0,
                "losses": 0,
                "last_played": datetime.now(UTC),
            }
        )

    await bot_module.leaderboard.callback(inter, queue="open")
    view = inter.followup.send.call_args.kwargs["view"]

    # Go to page 1 (last)
    btn_inter = _fake_interaction(admin, guild)
    await view._go(btn_inter, 1)
    assert view.page == 1

    # Try to go to page 2 (out of bounds): page does not change
    btn_inter2 = _fake_interaction(admin, guild)
    await view._go(btn_inter2, 2)
    assert view.page == 1, "Page must NOT change when going past total_pages"
    btn_inter2.followup.edit_message.assert_not_awaited()


# ── /elomodify ────────────────────────────────────────────────────
async def test_slash_elomodify_add():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    await bot_module.elomodify.callback(inter, queue="open", player=target, action="add", amount=50)

    col = bot_module.get_elo_col()
    doc = col.find_one({"_id": "2:open"})
    assert doc["elo"] == 2050


async def test_slash_elomodify_remove_floors_at_zero():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": "2:open",
            "user_id": "2",
            "queue_type": "open",
            "name": "Bob",
            "elo": 30,
            "wins": 0,
            "losses": 0,
        }
    )

    await bot_module.elomodify.callback(
        inter, queue="open", player=target, action="remove", amount=100
    )

    doc = col.find_one({"_id": "2:open"})
    assert doc["elo"] == 0  # max(0, 30 - 100)


# ── /resetelo ─────────────────────────────────────────────────────
async def test_slash_resetelo_single_player():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    target = _fake_member(2, "Bob")
    guild = _fake_guild(42, members=[admin, target])
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    col.insert_one(
        {
            "_id": "2:open",
            "user_id": "2",
            "queue_type": "open",
            "name": "Bob",
            "elo": 999,
            "wins": 50,
            "losses": 5,
        }
    )

    await bot_module.resetelo.callback(inter, queue="open", player=target, all_players=False)

    doc = col.find_one({"_id": "2:open"})
    assert doc["elo"] == bot_module.ELO_START
    assert doc["wins"] == 0
    assert doc["losses"] == 0


async def test_slash_resetelo_all_players():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    targets = [_fake_member(10 + i, f"P{i}") for i in range(5)]
    guild = _fake_guild(42, members=[admin] + targets)
    inter = _fake_interaction(admin, guild)

    col = bot_module.get_elo_col()
    for t in targets:
        col.insert_one(
            {
                "_id": f"{t.id}:open",
                "user_id": str(t.id),
                "queue_type": "open",
                "name": t.display_name,
                "elo": 100,
                "wins": 5,
                "losses": 1,
            }
        )

    await bot_module.resetelo.callback(inter, queue="open", player=None, all_players=True)

    for t in targets:
        doc = col.find_one({"_id": f"{t.id}:open"})
        assert doc["elo"] == bot_module.ELO_START
        assert doc["wins"] == 0


# ── /map ──────────────────────────────────────────────────────────
async def test_slash_map_returns_known_map():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(42, members=[admin])
    inter = _fake_interaction(admin, guild)

    await bot_module.map_pick.callback(inter)

    inter.response.send_message.assert_awaited_once()
    embed = inter.response.send_message.call_args.kwargs["embed"]
    # The title contains the map name
    assert any(m in embed.description for m in bot_module.MAPS), (
        f"Map not recognized: {embed.description}"
    )


# ── has_access ────────────────────────────────────────────────────
def test_has_access_admin_returns_true():
    import bot as bot_module

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(42, members=[admin])
    inter = _fake_interaction(admin, guild)

    assert bot_module.has_access(inter) is True


def test_has_access_non_admin_no_bypass_returns_false():
    import bot as bot_module

    user = _fake_member(1, "User", manage_guild=False)
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    assert bot_module.has_access(inter) is False


# ── /setup ────────────────────────────────────────────────────────
def _fake_guild_with_setup(guild_id: int = 42):
    g = _fake_guild(guild_id)
    g.categories = []
    g.text_channels = []

    async def _create_category(name):
        cat = MagicMock()
        cat.name = name
        g.categories.append(cat)
        return cat

    async def _create_text_channel(name, category=None):
        chan = MagicMock()
        chan.name = name
        chan.id = 100 + len(g.text_channels)
        chan.category = category
        chan.mention = f"#{name}"
        chan.send = AsyncMock(return_value=MagicMock(id=999))
        g.text_channels.append(chan)
        return chan

    g.create_category = AsyncMock(side_effect=_create_category)
    g.create_text_channel = AsyncMock(side_effect=_create_text_channel)
    return g


async def test_slash_setup_creates_category_and_channels():
    import bot as bot_module
    from cogs.admin import SETUP_CATEGORY_NAME, SETUP_CHANNELS

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild_with_setup(42)
    inter = _fake_interaction(admin, guild)

    # bot.get_cog returns None here (no cog loaded in tests)
    bot_module.bot.get_cog = MagicMock(return_value=None)

    await bot_module.setup_bot.callback(inter)

    # Category created
    assert any(c.name == SETUP_CATEGORY_NAME for c in guild.categories)
    # All channels created
    names = [c.name for c in guild.text_channels]
    for expected in SETUP_CHANNELS:
        assert expected in names

    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "Créés" in msg
    assert inter.followup.send.call_args.kwargs.get("ephemeral") is True


async def test_slash_setup_idempotent_when_channels_exist():
    import bot as bot_module
    from cogs.admin import SETUP_CATEGORY_NAME, SETUP_CHANNELS

    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild_with_setup(42)

    # Pre-create category + channels
    cat = MagicMock()
    cat.name = SETUP_CATEGORY_NAME
    guild.categories.append(cat)
    for n in SETUP_CHANNELS:
        chan = MagicMock()
        chan.name = n
        chan.id = 555
        chan.send = AsyncMock(return_value=MagicMock(id=999))
        chan.mention = f"#{n}"
        guild.text_channels.append(chan)

    inter = _fake_interaction(admin, guild)
    bot_module.bot.get_cog = MagicMock(return_value=None)

    await bot_module.setup_bot.callback(inter)

    # No creation
    guild.create_category.assert_not_awaited()
    guild.create_text_channel.assert_not_awaited()

    msg = inter.followup.send.call_args.args[0]
    assert "Déjà présents" in msg


def test_has_access_non_admin_with_bypass_role_returns_true():
    import bot as bot_module

    role = MagicMock()
    role.id = 555
    user = _fake_member(1, "User", manage_guild=False)
    user.roles = [role]
    guild = _fake_guild(42, members=[user])
    inter = _fake_interaction(user, guild)

    bot_module.set_bypass_role(42, 555)
    assert bot_module.has_access(inter) is True
