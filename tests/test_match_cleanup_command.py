"""Tests for the /match-cleanup admin slash command (Task 12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_cog_with_db(matches_doc=None):
    from cogs.match._cog import MatchCog

    bot = MagicMock()
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=MagicMock())
    db["matches"].find_one = MagicMock(return_value=matches_doc)
    db["matches"].update_one = MagicMock()
    # MatchCog(bot, db, *, rng=None, henrik_client=None)
    cog = MatchCog(bot, db)
    return cog


@pytest.mark.asyncio
async def test_match_cleanup_rejects_non_admin():
    cog = _build_cog_with_db()
    interaction = MagicMock()
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    await cog.match_cleanup.callback(cog, interaction, match_id="m1")

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    msg = (args[0] if args else kwargs.get("content", "")).lower()
    assert ("admin" in msg) or ("permission" in msg)
    assert kwargs.get("ephemeral", False) is True


@pytest.mark.asyncio
async def test_match_cleanup_match_not_found():
    cog = _build_cog_with_db(matches_doc=None)
    interaction = MagicMock()
    interaction.user.guild_permissions.administrator = True
    interaction.response.send_message = AsyncMock()

    await cog.match_cleanup.callback(cog, interaction, match_id="unknown")

    args, kwargs = interaction.response.send_message.await_args
    msg = (args[0] if args else kwargs.get("content", "")).lower()
    assert "introuvable" in msg or "not found" in msg


@pytest.mark.asyncio
async def test_match_cleanup_legacy_match_without_category_id():
    cog = _build_cog_with_db(matches_doc={"_id": "old", "status": "pending"})
    interaction = MagicMock()
    interaction.user.guild_permissions.administrator = True
    interaction.response.send_message = AsyncMock()

    await cog.match_cleanup.callback(cog, interaction, match_id="old")

    args, kwargs = interaction.response.send_message.await_args
    msg = (args[0] if args else kwargs.get("content", "")).lower()
    assert "category_id" in msg or "pre-migration" in msg


@pytest.mark.asyncio
async def test_match_cleanup_happy_path(monkeypatch):
    from cogs.match import _cog as match_cog_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    cog = _build_cog_with_db(
        matches_doc={
            "_id": "m1",
            "status": "contested",
            "category_id": 4242,
            "match_number": 7,
        }
    )
    interaction = MagicMock()
    interaction.user.id = 1
    interaction.user.guild_permissions.administrator = True
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    await cog.match_cleanup.callback(cog, interaction, match_id="m1")

    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 4242

    # update_one is called twice: (1) mark_match_cleanup_started which
    # sets delete_started_at before the Discord call, (2) the terminal
    # status transition.
    assert cog.db["matches"].update_one.call_count == 2
    cleanup_started_call, status_call = cog.db["matches"].update_one.call_args_list
    assert "delete_started_at" in cleanup_started_call.args[1]["$set"]
    set_payload = status_call.args[1]["$set"]
    assert set_payload["status"] == "cleaned_up"
    assert "cleaned_up_by" in set_payload
    assert "cleaned_up_at" in set_payload


def test_resolve_match_id_converts_hex_and_falls_back():
    """The ObjectId hex string must be converted; any other value is
    returned as is (compat with legacy docs whose _id is a string)."""
    from bson import ObjectId

    from cogs.match._cog import MatchCog

    oid = ObjectId()
    resolved = MatchCog._resolve_match_id(str(oid))
    assert isinstance(resolved, ObjectId)
    assert resolved == oid
    assert MatchCog._resolve_match_id("not-an-objectid") == "not-an-objectid"


@pytest.mark.asyncio
async def test_match_cleanup_queries_by_objectid(monkeypatch):
    """Regression: a real match (created via repository.create_match) has
    an ObjectId _id. The command must convert the typed hex into an
    ObjectId, otherwise find_one never matches and the match stays
    "not found"."""
    from bson import ObjectId

    from cogs.match import _cog as match_cog_module

    monkeypatch.setattr(match_cog_module, "delete_match_category", AsyncMock())

    oid = ObjectId()
    cog = _build_cog_with_db(matches_doc={"_id": oid, "status": "contested", "category_id": 4242})
    interaction = MagicMock()
    interaction.user.id = 1
    interaction.user.guild_permissions.administrator = True
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()

    await cog.match_cleanup.callback(cog, interaction, match_id=str(oid))

    # find_one must be queried with an ObjectId, not the raw hex string.
    find_call = cog.db["matches"].find_one.call_args
    assert find_call.args[0] == {"_id": oid}
    assert isinstance(find_call.args[0]["_id"], ObjectId)
    # Subsequent operations target the real _id of the found doc.
    status_call = cog.db["matches"].update_one.call_args_list[-1]
    assert status_call.args[0] == {"_id": oid}
