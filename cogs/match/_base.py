"""Shared attribute declarations for the MatchCog behaviour mixins.

The match flow is split across mixins (``_formation``, ``_verification``,
``_lifecycle``) that are combined into the concrete ``MatchCog``. Those
mixins read instance attributes (``self.db``, ``self.bot``, …) that are
assigned in ``MatchCog.__init__``. This base declares them — typing only,
no runtime behaviour — so each mixin type-checks in isolation.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any

from discord.ext import commands

from cogs.match._vote import VoteView
from services.riot_api import HenrikDevClient


class MatchCogState:
    """Declares the instance attributes set in ``MatchCog.__init__``."""

    bot: commands.Bot
    db: Any
    rng: random.Random
    henrik_client: HenrikDevClient | None
    vote_view: VoteView
    _henrik_consecutive_failures: int
    _henrik_circuit_open_until: datetime | None
    _henrik_lock: asyncio.Lock
    _guild_member_edit_sem: asyncio.Semaphore
    # Defined on the concrete ``MatchCog`` (the periodic loop and the
    # contested-match expiry it drives); declared here so ``LifecycleMixin``
    # type-checks in isolation.
    _timeout_loop: Any

    async def expire_stale_contested_matches(self, *, now: datetime | None = None) -> int:
        raise NotImplementedError
