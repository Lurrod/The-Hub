"""Tests for the admin cog — focus on the `_has_access` authorization
guard (security-critical) and the input-validation / permission branches
of /bypass, /map and /clear."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.admin import AdminCog, _has_access
from services import repository

GUILD_ID = 42


def _fake_interaction(
    *, manage_guild: bool, role_ids: tuple[int, ...] = (), guild_id: int = GUILD_ID
):
    inter = MagicMock()
    inter.guild_id = guild_id
    inter.user.guild_permissions.manage_guild = manage_guild
    inter.user.display_name = "Admin"
    inter.user.roles = [MagicMock(id=rid) for rid in role_ids]
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


@pytest.fixture
def db():
    import bot

    return bot.db


# ── _has_access (authorization guard) ─────────────────────────────────────
def test_has_access_manage_guild_true(db):
    inter = _fake_interaction(manage_guild=True)
    assert _has_access(inter, db) is True


def test_has_access_no_perms_no_bypass_configured(db):
    inter = _fake_interaction(manage_guild=False)
    assert _has_access(inter, db) is False


def test_has_access_bypass_role_match(db):
    repository.set_bypass_role(db, GUILD_ID, 999)
    inter = _fake_interaction(manage_guild=False, role_ids=(999,))
    assert _has_access(inter, db) is True


def test_has_access_bypass_role_configured_but_user_lacks_it(db):
    repository.set_bypass_role(db, GUILD_ID, 999)
    inter = _fake_interaction(manage_guild=False, role_ids=(123, 456))
    assert _has_access(inter, db) is False


# ── /map ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_map_pick_denied_without_access(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=False)
    await cog.map_pick.callback(cog, inter)
    msg = inter.response.send_message.await_args.args[0]
    assert "permission" in msg.lower()


@pytest.mark.asyncio
async def test_map_pick_allowed_with_manage_guild(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    await cog.map_pick.callback(cog, inter)
    _, kwargs = inter.response.send_message.call_args
    assert kwargs["embed"].title.startswith("🗺️")


# ── /coinflip (no access required) ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_coinflip_sends_embed(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=False)
    await cog.coinflip.callback(cog, inter)
    _, kwargs = inter.response.send_message.call_args
    assert kwargs["embed"].description.lstrip("# ") in ("Pile", "Face")


# ── /clear ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_clear_denied_without_access(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=False)
    await cog.clear.callback(cog, inter, 10)
    assert "permission" in inter.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, -5, 101, 500])
async def test_clear_rejects_out_of_range_amount(db, amount):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    await cog.clear.callback(cog, inter, amount)
    assert "entre 1 et 100" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_clear_happy_path_purges_and_reports(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    inter.channel.purge = AsyncMock(return_value=[object(), object(), object()])
    await cog.clear.callback(cog, inter, 3)
    inter.channel.purge.assert_awaited_once_with(limit=3)
    _, kwargs = inter.followup.send.call_args
    assert "3" in kwargs["embed"].description


# ── /bypass validation ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bypass_rejects_everyone(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    role = MagicMock()
    role.id = GUILD_ID  # @everyone role id == guild id
    role.is_default.return_value = True
    role.managed = False
    await cog.bypass.callback(cog, inter, role)
    assert "everyone" in inter.response.send_message.await_args.args[0].lower()
    assert repository.get_bypass_role(db, GUILD_ID) is None


@pytest.mark.asyncio
async def test_bypass_rejects_managed_role(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    role = MagicMock()
    role.id = 777
    role.is_default.return_value = False
    role.managed = True
    await cog.bypass.callback(cog, inter, role)
    assert "intégration" in inter.response.send_message.await_args.args[0].lower()
    assert repository.get_bypass_role(db, GUILD_ID) is None


# ── /help ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_help_members_needs_no_access(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=False)
    await cog.help_cmd.callback(cog, inter, "members")
    _, kwargs = inter.response.send_message.call_args
    assert kwargs["embed"].title.startswith("📖")


@pytest.mark.asyncio
async def test_help_admin_denied_without_access(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=False)
    await cog.help_cmd.callback(cog, inter, "admin")
    assert "permission" in inter.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_help_admin_allowed_with_access(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    await cog.help_cmd.callback(cog, inter, "admin")
    _, kwargs = inter.response.send_message.call_args
    assert kwargs["embed"].title.startswith("⚙️")
    assert any(f.name == "/bypass @role" for f in kwargs["embed"].fields)


@pytest.mark.asyncio
async def test_bypass_happy_path_persists_role(db):
    cog = AdminCog(MagicMock(), db)
    inter = _fake_interaction(manage_guild=True)
    role = MagicMock()
    role.id = 555
    role.is_default.return_value = False
    role.managed = False
    role.mention = "@Staff"
    await cog.bypass.callback(cog, inter, role)
    assert repository.get_bypass_role(db, GUILD_ID) == 555
    _, kwargs = inter.response.send_message.call_args
    assert kwargs["embed"].title.startswith("🔓")
