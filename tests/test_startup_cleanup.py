"""Integration test for cog_load orphan cleanup (Task 13)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cog_load_deletes_orphan_categories(monkeypatch):
    """cog_load must query in-progress + validated + contested match
    category_ids and call cleanup_orphan_match_categories per guild.
    Uses the real status strings persisted by the bot (pending /
    validated_a / validated_b / contested), not synthetic ones."""
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    cleanup_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    guild_a = MagicMock(name="GuildA")
    guild_b = MagicMock(name="GuildB")
    bot.guilds = [guild_a, guild_b]

    db = MagicMock()

    def fake_find(query, *args, **kwargs):
        # Only the active-status query returns the seed docs. Recovery
        # (preparing) and in-flight-cleanup queries return empty here.
        if "status" in query and query["status"] == {
            "$in": ["pending", "validated_a", "validated_b", "contested"]
        }:
            return iter(
                [
                    {"category_id": 100, "status": "pending"},
                    {"category_id": 200, "status": "contested"},
                    {"category_id": 300, "status": "validated_a"},
                ]
            )
        return iter([])

    db["matches"].find = MagicMock(side_effect=fake_find)

    cog = MatchCog(bot, db)
    await cog.cog_load()

    # Called once per guild
    assert cleanup_mock.await_count == 2
    # pending + contested + validated_a category IDs all protected
    first_call_kwargs = cleanup_mock.await_args_list[0].kwargs
    assert first_call_kwargs["active_category_ids"] == {100, 200, 300}

    # Verify the Mongo filter targets the right statuses. cog_load now
    # emits multiple find() queries (preparing recovery, in-flight
    # cleanup per-guild, active_ids). We want the one with a $in filter
    # on status -- the active_ids computation.
    status_call = next(
        c
        for c in db["matches"].find.call_args_list
        if isinstance(c.args[0].get("status"), dict) and "$in" in c.args[0]["status"]
    )
    status_filter = status_call.args[0]["status"]["$in"]
    assert set(status_filter) == {"pending", "validated_a", "validated_b", "contested"}


@pytest.mark.asyncio
async def test_cog_load_handles_empty_active_set(monkeypatch):
    """When no active matches exist, cleanup is called with empty set."""
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    cleanup_mock = AsyncMock(return_value=5)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    bot.guilds = [MagicMock()]
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    await cog.cog_load()

    cleanup_mock.assert_awaited_once()
    assert cleanup_mock.await_args.kwargs["active_category_ids"] == set()


@pytest.mark.asyncio
async def test_cog_load_per_guild_error_does_not_block_others(monkeypatch):
    """A cleanup error in one guild must not stop processing of other guilds."""
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    cleanup_mock = AsyncMock(side_effect=[RuntimeError("boom"), 3])
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    bot.guilds = [MagicMock(name="bad"), MagicMock(name="good")]
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    # Must not raise
    await cog.cog_load()

    # Second guild still got processed
    assert cleanup_mock.await_count == 2


@pytest.mark.asyncio
async def test_cog_load_excludes_in_flight_cleanup_categories(monkeypatch):
    """Recovery after a partial crash: a match whose cleanup has been
    started (delete_started_at set) but whose status is still active
    must be removed from active_category_ids so orphan_cleanup can
    finish the job."""
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    cleanup_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)
    # 200 is marked in-flight cleanup -> must be removed despite
    # its active status.
    monkeypatch.setattr(
        match_cog_module.repository,
        "find_category_ids_with_cleanup_started",
        lambda db, *, origin_guild_id: {200},
    )

    bot = MagicMock()
    guild = MagicMock()
    guild.id = 42
    bot.guilds = [guild]

    db = MagicMock()

    def fake_find(query, *args, **kwargs):
        if query.get("status") == "preparing":
            return iter([])
        return iter(
            [
                {"category_id": 100, "status": "pending"},
                {"category_id": 200, "status": "validated_a"},  # in-flight cleanup
                {"category_id": 300, "status": "contested"},
            ]
        )

    db["matches"].find = MagicMock(side_effect=fake_find)

    cog = MatchCog(bot, db)
    await cog.cog_load()

    passed_ids = cleanup_mock.await_args.kwargs["active_category_ids"]
    assert passed_ids == {100, 300}
    assert 200 not in passed_ids


@pytest.mark.asyncio
async def test_cog_load_recovers_preparing_matches(monkeypatch):
    """Bot restart during captain draft or map ban leaves a match doc
    in status='preparing'. On startup the cog must atomically cancel
    the doc and delete its Discord category (the in-memory session
    is dead, button views are not persistent).
    """
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)
    monkeypatch.setattr(
        match_cog_module, "cleanup_orphan_match_categories", AsyncMock(return_value=0)
    )

    cancelled_ids: list = []

    def fake_cancel(db, match_id):
        cancelled_ids.append(match_id)
        return {"_id": match_id, "status": "preparing"}

    monkeypatch.setattr(match_cog_module.repository, "cancel_preparing_match", fake_cancel)

    bot = MagicMock()
    guild = MagicMock()
    guild.id = 42
    bot.guilds = [guild]
    bot.get_guild = MagicMock(return_value=guild)

    db = MagicMock()

    def fake_find(query, *args, **kwargs):
        if query.get("status") == "preparing":
            return iter(
                [
                    {
                        "_id": "match-A",
                        "category_id": 7777,
                        "origin_guild_id": 42,
                        "match_number": 1,
                    },
                    {
                        "_id": "match-B",
                        "category_id": 8888,
                        "origin_guild_id": 42,
                        "match_number": 2,
                    },
                ]
            )
        return iter([])

    db["matches"].find = MagicMock(side_effect=fake_find)

    cog = MatchCog(bot, db)
    await cog.cog_load()

    # Both preparing matches were cancelled at the DB layer.
    assert cancelled_ids == ["match-A", "match-B"]
    # Both categories were deleted on Discord.
    assert delete_mock.await_count == 2
    deleted_cat_ids = {c.kwargs["category_id"] for c in delete_mock.await_args_list}
    assert deleted_cat_ids == {7777, 8888}


@pytest.mark.asyncio
async def test_cog_load_preparing_recovery_continues_on_per_doc_error(monkeypatch):
    """One bad preparing doc must not block recovery of the others."""
    from cogs.match import _lifecycle as match_cog_module
    from cogs.match._cog import MatchCog

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)
    monkeypatch.setattr(
        match_cog_module, "cleanup_orphan_match_categories", AsyncMock(return_value=0)
    )

    call_count = {"n": 0}

    def fake_cancel(db, match_id):
        call_count["n"] += 1
        if match_id == "bad":
            raise RuntimeError("DB blip")
        return {"_id": match_id, "status": "preparing"}

    monkeypatch.setattr(match_cog_module.repository, "cancel_preparing_match", fake_cancel)

    bot = MagicMock()
    guild = MagicMock()
    guild.id = 42
    bot.guilds = [guild]
    bot.get_guild = MagicMock(return_value=guild)

    db = MagicMock()

    def fake_find(query, *args, **kwargs):
        if query.get("status") == "preparing":
            return iter(
                [
                    {"_id": "bad", "category_id": 1, "origin_guild_id": 42},
                    {"_id": "ok", "category_id": 2, "origin_guild_id": 42},
                ]
            )
        return iter([])

    db["matches"].find = MagicMock(side_effect=fake_find)

    cog = MatchCog(bot, db)
    await cog.cog_load()

    # Both attempts ran (the bad one's error did not abort the loop).
    assert call_count["n"] == 2
    # The OK doc still got its category deleted.
    deleted_cat_ids = {c.kwargs["category_id"] for c in delete_mock.await_args_list}
    assert 2 in deleted_cat_ids


@pytest.mark.asyncio
async def test_cog_load_with_real_bot_defers_cleanup_to_after_ready(monkeypatch):
    """With a real commands.Bot instance, cog_load must NOT run the
    sweep synchronously: setup_hook runs before on_ready -> bot.guilds
    is empty. Cleanup is scheduled as a background task that awaits
    wait_until_ready first.
    """
    from discord.ext import commands

    from cogs.match._cog import MatchCog

    run_cleanup_mock = AsyncMock()
    monkeypatch.setattr(MatchCog, "_run_startup_cleanup", run_cleanup_mock)

    # A subclassed Bot to satisfy isinstance() without booting a gateway.
    class _StubBot(commands.Bot):
        def __init__(self) -> None:
            self._scheduled: list = []
            self._ready_waited = False

        async def wait_until_ready(self) -> None:
            self._ready_waited = True

        class _Loop:
            def __init__(self, outer: _StubBot) -> None:
                self.outer = outer

            def create_task(self, coro):
                self.outer._scheduled.append(coro)
                return coro

        @property
        def loop(self):
            return _StubBot._Loop(self)

        @property
        def guilds(self):
            return []

    bot = _StubBot()
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    # Stub the timeout loop so cog_load doesn't try to start it.
    monkeypatch.setattr(cog, "_timeout_loop", MagicMock())

    await cog.cog_load()

    # Cleanup must NOT have run inline: it's now deferred.
    run_cleanup_mock.assert_not_awaited()
    # Exactly one background task scheduled (the deferred cleanup).
    assert len(bot._scheduled) == 1

    # Drain the scheduled coroutine and verify it awaits ready then
    # delegates to _run_startup_cleanup.
    await bot._scheduled[0]
    assert bot._ready_waited is True
    run_cleanup_mock.assert_awaited_once()
