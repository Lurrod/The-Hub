"""Cog load/unload + startup cleanup & recovery for MatchCog."""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from cogs.match._base import MatchCogState
from services import repository
from services.match_category import (
    cleanup_orphan_match_categories,
    delete_match_category,
)

logger = logging.getLogger(__name__)


class LifecycleMixin(MatchCogState):
    # Statuses for which the match's Discord category must be preserved
    # by the orphan cleanup at boot. Covers:
    #   - "pending"      : match in progress, vote open
    #   - "validated_a"  : team A won but ELO not yet applied (Henrik
    #                      verification deferred by HENRIK_VERIFY_DELAY_MINUTES)
    #   - "validated_b"  : same for team B
    #   - "contested"    : vote timeout, awaiting admin resolution
    # Terminal statuses ("cancelled", "cleaned_up") are NOT protected:
    # their categories must disappear at boot if they are still hanging around.
    _ACTIVE_MATCH_STATUSES: tuple[str, ...] = (
        "pending",
        "validated_a",
        "validated_b",
        "contested",
    )

    async def cog_load(self) -> None:
        # `_timeout_loop.start()` calls `_before_loop` which does
        # `await self.bot.wait_until_ready()`. In tests, `self.bot` is a
        # MagicMock whose `wait_until_ready` attribute is not awaitable
        # -> TypeError silently logged by `tasks.Loop`. We detect this
        # case and skip the start in tests (the timeout-loop only makes
        # sense with a live Discord gateway anyway).
        if isinstance(self.bot, commands.Bot):
            self._timeout_loop.start()
            # cog_load runs inside setup_hook(), which is BEFORE on_ready
            # -> self.bot.guilds is empty here. Defer the actual cleanup
            # to a background task that awaits wait_until_ready first.
            self.bot.loop.create_task(self._deferred_startup_cleanup())
        else:
            # Tests: bot is a MagicMock with synthetic .guilds. Run the
            # cleanup inline so assertions can observe its effects
            # (no real gateway -> wait_until_ready is meaningless).
            await self._run_startup_cleanup()

    async def _deferred_startup_cleanup(self) -> None:
        """Wait for the gateway READY (so self.bot.guilds is populated)
        then run the startup cleanup. Errors are swallowed and logged:
        a failed sweep must not crash the bot's task loop.
        """
        try:
            await self.bot.wait_until_ready()
            await self._run_startup_cleanup()
        except Exception:
            logger.exception("[match] deferred startup cleanup failed")

    async def _run_startup_cleanup(self) -> None:
        """Startup recovery + orphan-category sweep.

        Order matters:
          1. Expire stale 'contested' matches so their categories drop
             out of the active set.
          2. Recover 'preparing' matches: a bot restart mid-draft or
             mid-map-ban kills the in-memory session, leaving dead
             buttons and a stuck category. We mark these cancelled and
             delete the category. This MUST run BEFORE computing
             active_ids so the now-cancelled docs don't protect a
             category from orphan cleanup later.
          3. Sweep orphan 'Match #N' categories not referenced by any
             active match.
        """
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] _run_startup_cleanup expire_stale_contested raised")

        await self._recover_preparing_matches()

        active_ids: set[int] = {
            m["category_id"]
            for m in self.db["matches"].find(
                {
                    "status": {"$in": list(self._ACTIVE_MATCH_STATUSES)},
                    "elo_applied": {"$ne": True},
                },
                {"category_id": 1},
            )
            if m.get("category_id")
        }
        for guild in self.bot.guilds:
            # Safety net: if a previous cleanup was interrupted between
            # `mark_match_cleanup_started` and the terminal status
            # transition, we remove those categories from the active set.
            # Orphan cleanup will resume `delete_match_category` (idempotent).
            in_flight_cleanup = repository.find_category_ids_with_cleanup_started(
                self.db, origin_guild_id=guild.id
            )
            guild_active_ids = active_ids - in_flight_cleanup
            try:
                deleted = await cleanup_orphan_match_categories(
                    guild=guild, active_category_ids=guild_active_ids
                )
                logger.info(
                    "[match] Startup cleanup in %s: %d orphan categories deleted",
                    guild.name,
                    deleted,
                )
            except Exception:
                logger.exception("[match] cog_load cleanup failed for guild %s", guild.name)

    async def _recover_preparing_matches(self) -> None:
        """Find every match stuck in status='preparing' (i.e. a draft
        or map-ban session that was running when the bot last shut
        down), atomically transition it to 'cancelled', and delete its
        Discord category. The in-memory session can't be restored
        (button views are not persistent), so leaving the category
        would mean dead buttons and an undeletable channel for admins
        (since /match-cancel previously could not find the match).
        """
        try:
            preparing = await asyncio.to_thread(repository.find_preparing_matches, self.db)
        except Exception:
            logger.exception("[match] find_preparing_matches raised")
            return

        for m in preparing:
            match_id = m.get("_id")
            cat_id = m.get("category_id")
            guild_id = m.get("origin_guild_id")
            try:
                await asyncio.to_thread(repository.cancel_preparing_match, self.db, match_id)
                if cat_id and guild_id:
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is not None:
                        await delete_match_category(
                            guild=guild,
                            category_id=int(cat_id),
                            reason="Bot restart during draft/map-ban",
                        )
                logger.info(
                    "[match] Recovered preparing match #%s (cancelled + category deleted)",
                    m.get("match_number"),
                )
            except Exception:
                logger.exception("[match] failed to recover preparing match _id=%s", match_id)

    async def cog_unload(self):
        self._timeout_loop.cancel()
