"""Vote-timeout + Henrik result verification flow for MatchCog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord

from cogs.match._base import MatchCogState
from cogs.match._constants import (
    _QUEUE_LABEL_BY_TYPE,
    ADMIN_ROLE_NAMES,
    HENRIK_CIRCUIT_FAIL_THRESHOLD,
    HENRIK_CIRCUIT_OPEN_MINUTES,
    HENRIK_VERIFY_DELAY_MINUTES,
    HENRIK_VERIFY_TIMEOUT_MINUTES,
    MAJORITY_THRESHOLD,
    MATCH_HOST_ROLE_NAME,
    RESULTS_CHANNELS,
    VOTE_TIMEOUT_MINUTES,
)
from cogs.match._embeds import (
    build_elo_changes_embed,
)
from services import repository
from services.elo_updater import (
    apply_match_validation,
    build_elo_results,
)
from services.leaderboard_refresh import refresh_leaderboard_channel
from services.match_category import (
    delete_match_category,
)
from services.match_verifier import (
    build_extended_stats,
    compute_round_breakdown,
    compute_team_scores,
    find_henrik_custom_match,
    ratings_by_uid,
)
from services.rating import RatingInputs, compute_rating_2_0
from services.scoreboard_img import generate_scoreboard

logger = logging.getLogger(__name__)


class VerificationMixin(MatchCogState):
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Vote validated: we do NOT touch ELO yet.
        ELO will be applied in a single pass by `_verify_match`
        after ~HENRIK_VERIFY_DELAY_MINUTES (with ACS weighting if
        HenrikDev found the custom, flat otherwise).
        """
        guild = getattr(inter, "guild", None)

        # Best-effort announcement.
        if guild is None:
            return

        # Deletion of the match's Discord category.
        # Graceful: legacy matches without category_id are ignored.
        category_id = match_doc.get("category_id")
        if category_id:
            await asyncio.to_thread(
                repository.mark_match_cleanup_started, self.db, match_doc["_id"]
            )
            await delete_match_category(
                guild=guild,
                category_id=category_id,
                reason=(f"Match #{match_doc.get('match_number', '?')} vote validé"),
            )

        try:
            elo_log_channel = discord.utils.get(
                guild.text_channels,
                name="elo-adding",
            )
        except Exception:
            logger.exception("[match] elo-adding lookup raised")
            return
        if elo_log_channel is None:
            return
        try:
            await elo_log_channel.send(
                f"⏳ Match validé ({match_doc.get('status')}). "
                f"Vérification HenrikDev dans {HENRIK_VERIFY_DELAY_MINUTES} min "
                f"(nouvel essai chaque minute, abandon à {HENRIK_VERIFY_TIMEOUT_MINUTES} min)."
            )
        except discord.Forbidden:
            # The bot does not have Send Messages permission in #elo-adding.
            # This is a config issue that the operator should pick up.
            logger.warning(
                "[match] Henrik announcement send denied (Forbidden) on #%s "
                "guild=%s - check the bot permissions.",
                elo_log_channel.name,
                guild.id,
            )
        except discord.HTTPException:
            # Transient Discord error (5xx, rate limit). We log but do
            # not fail the ELO flow.
            logger.exception("[match] Henrik announcement HTTP error")
        except Exception:
            logger.exception("[match] Henrik wait announcement send raised")

    # ── Vote timeouts ────────────────────────────────────────────
    async def check_vote_timeouts(self, *, now: datetime | None = None) -> int:
        """
        Scan every known guild. For each `pending` match created more
        than VOTE_TIMEOUT_MINUTES ago, mark it `contested` and ping
        the channel's admin role.

        Returns:
            number of matches moved to `contested` during this call.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(minutes=VOTE_TIMEOUT_MINUTES)

        # Per-guild parallelization: with N guilds, sequential execution
        # would wait for each guild's scan + transitions to finish before
        # moving to the next. asyncio.gather makes the tick bounded by
        # the slowest guild, not by their sum.
        results = await asyncio.gather(
            *[self._check_vote_timeouts_for_guild(g, cutoff) for g in self.bot.guilds],
            return_exceptions=True,
        )
        flagged = 0
        for r in results:
            if isinstance(r, BaseException):
                logger.info(f"[match] check_vote_timeouts (guild) raised: {r!r}")
                continue
            flagged += r
        return flagged

    async def _check_vote_timeouts_for_guild(self, guild, cutoff: datetime) -> int:
        flagged = 0
        col = repository.get_matches_col(self.db)

        # Scan in a thread: `find().toList()` is blocking and can iterate
        # over N matches, freezing the Discord event loop.
        def _fetch_stale() -> list[Mapping[str, Any]]:
            return list(
                col.find(
                    {
                        "status": "pending",
                        "created_at": {"$lt": cutoff},
                        "origin_guild_id": guild.id,
                    }
                )
            )

        stale = await asyncio.to_thread(_fetch_stale)
        for match in stale:
            # Atomic re-fetch just before the transition to avoid a race
            # with a vote that would cross the threshold between the
            # initial scan and now. Without this re-fetch, we would read
            # `match.get("votes")` from the stale snapshot -> the tick
            # could transition pending->contested while a concurrent vote
            # just reached the majority, leaving the match stuck in
            # `contested` with ELO never applied.
            fresh = await asyncio.to_thread(col.find_one, {"_id": match["_id"]})
            if not fresh or fresh.get("status") != "pending":
                continue
            votes = fresh.get("votes", {})
            count_a = sum(1 for v in votes.values() if v == "a")
            count_b = sum(1 for v in votes.values() if v == "b")
            # Auto-repair: we backdate `validated_at` to the match's
            # `created_at`. Without this backdate, the Henrik delay
            # (~5 min after validated_at) would restart from 0; the
            # match was however already created > VOTE_TIMEOUT_MINUTES
            # ago, the HenrikDev custom is already indexed and the
            # verification can run immediately on the next tick.
            repaired_validated_at = fresh.get("created_at")
            if count_a >= MAJORITY_THRESHOLD:
                # A match may have reached 7+ votes without transitioning
                # (e.g. bot crash between vote write and set_match_status).
                # We catch up; check_henrik_verifications will apply
                # the ELO on the next tick.
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status="validated_a",
                    validated_at=repaired_validated_at,
                )
                continue
            if count_b >= MAJORITY_THRESHOLD:
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status="validated_b",
                    validated_at=repaired_validated_at,
                )
                continue
            transitioned = await asyncio.to_thread(
                repository.transition_match_status,
                self.db,
                match["_id"],
                from_status="pending",
                to_status="contested",
            )
            if transitioned is None:
                continue
            await self._handle_timeout(guild, match)
            flagged += 1
        return flagged

    async def _handle_timeout(self, guild, match) -> None:
        # Note: the transition to "contested" is done by
        # check_vote_timeouts via transition_match_status (atomic CAS).
        # We only enter here if the transition succeeded.

        # Revoke Match Host role; no longer governed by deferred cleanup.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader_member = guild.get_member(int(leader_id))
            if leader_member is not None:
                host_role = discord.utils.get(guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader_member.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader_member.remove_roles(
                            host_role, reason="Vote expiré : rôle Match Host retiré"
                        )

        admin_role = None
        for role_name in ADMIN_ROLE_NAMES:
            admin_role = discord.utils.get(guild.roles, name=role_name)
            if admin_role:
                break

        channel_id = match.get("channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return

        ping = admin_role.mention if admin_role else "@admin"
        votes = match.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        try:
            await channel.send(
                f"⏰ {ping} Le vote du match a expiré (>{VOTE_TIMEOUT_MINUTES} min "
                f"sans {MAJORITY_THRESHOLD}/10). Score actuel : Team A `{count_a}` / Team B `{count_b}`. "
                f"Validation manuelle requise.",
            )
        except Exception:
            logger.exception("[match] _handle_timeout send raised")

    # ── HenrikDev verification + single ELO application ──────────
    async def check_henrik_verifications(self, *, now: datetime | None = None) -> int:
        """For each match validated > HENRIK_VERIFY_DELAY_MINUTES ago without
        Henrik verification:
          - look up the HenrikDev custom (ACS multipliers if found)
          - if Henrik finds it: apply weighted ELO (final)
          - if Henrik does not find it and we are under the timeout: we will retry
            on the next tick (1 min loop)
          - if we passed HENRIK_VERIFY_TIMEOUT_MINUTES: apply flat ELO
            and mark the match as verified (Henrik given up)
        Returns the number of matches processed."""
        now = now or datetime.now(UTC)
        start_cutoff = now - timedelta(minutes=HENRIK_VERIFY_DELAY_MINUTES)

        # Per-guild parallelization: same principle as check_vote_timeouts.
        results = await asyncio.gather(
            *[
                self._check_henrik_verifications_for_guild(g, start_cutoff, now=now)
                for g in self.bot.guilds
            ],
            return_exceptions=True,
        )
        processed = 0
        for r in results:
            if isinstance(r, BaseException):
                logger.info(f"[match] check_henrik_verifications (guild) raised: {r!r}")
                continue
            processed += r
        return processed

    async def _check_henrik_verifications_for_guild(
        self,
        guild,
        start_cutoff: datetime,
        *,
        now: datetime | None = None,
    ) -> int:
        processed = 0
        # Blocking scan -> thread to avoid freezing the event loop.
        stale = await asyncio.to_thread(
            repository.find_validated_unverified,
            self.db,
            start_cutoff,
            origin_guild_id=guild.id,
        )
        for match in stale:
            try:
                await self._verify_match(guild, match, now=now)
            except Exception:
                logger.exception("[match] verify_match raised")
            processed += 1
        return processed

    async def _verify_match(
        self,
        guild,
        match_doc: dict,
        *,
        now: datetime | None = None,
        force_apply: bool = False,
    ) -> None:
        """
        Verify the match against Henrik and apply ELO.

        Flow:
          1. Try Henrik first. If the custom is found, apply flat ELO,
             mark `henrik_verified=True, found=True`, post the scoreboard
             and persist extended stats.
          2. If Henrik did not find the custom AND we are still within
             HENRIK_VERIFY_TIMEOUT_MINUTES of `validated_at`, do nothing —
             the next tick (1 min loop) will retry. Henrik typically
             indexes customs 10-30 min after they end, so retrying is
             essential to give the scoreboard a chance.
          3. If Henrik never responded AND the timeout has elapsed (or
             `force_apply=True`), apply flat ELO and mark
             `henrik_verified=True, found=False` (no scoreboard).

        Idempotency: we **claim** the match (`elo_applied=True`) BEFORE
        applying the ELO. If the claim fails (already applied elsewhere),
        we skip. If the ELO application raises, we release the claim to
        allow a retry on the next tick.

        `force_apply=True` bypasses the retry-window check. Used by
        tests that want to assert the flat-fallback path without
        manipulating `validated_at`.
        """
        queue_type = match_doc.get("queue_type", "open")
        now = now or datetime.now(UTC)

        # Try Henrik BEFORE claiming the match. If Henrik has not indexed
        # the custom yet and we are still within the retry window, we
        # must leave the match doc untouched so the next tick retries.
        try:
            fetched = await self._fetch_henrik_match_summary(guild, match_doc)
        except Exception:
            logger.exception("[match] _fetch_henrik_match_summary raised")
            fetched = None

        if fetched is None and not force_apply:
            validated_at = match_doc.get("validated_at")
            if validated_at is not None:
                elapsed = now - validated_at
                if elapsed < timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES):
                    # Within retry window — leave the match alone, the
                    # next 1-min tick will try Henrik again.
                    return

        # Atomic claim: only the first call goes through. Prevents double
        # application in case of a crash between apply_match_validation
        # and set_match_henrik_verified, or a concurrent tick.
        claimed = await asyncio.to_thread(
            repository.claim_match_for_elo,
            self.db,
            match_doc["_id"],
        )
        if claimed is None:
            return  # Already applied by a previous tick.

        # Draft queue: no ELO, no points, no leaderboard. We only mark the
        # match processed and post the end-of-game scoreboard (with zero ELO
        # deltas). The claim above guarantees this runs exactly once.
        if queue_type == "draft":
            await asyncio.to_thread(
                repository.set_match_henrik_verified,
                self.db,
                match_doc["_id"],
                found=(fetched is not None),
                multipliers=None,
            )
            if fetched is not None:
                summary, team_a_uid_by_puuid, team_b_uid_by_puuid = fetched
                try:
                    score_a, score_b = compute_team_scores(
                        summary, team_a_uid_by_puuid, team_b_uid_by_puuid
                    )
                    if score_a is not None and score_b is not None:
                        await asyncio.to_thread(
                            repository.set_match_score,
                            self.db,
                            match_doc["_id"],
                            score_a,
                            score_b,
                        )
                except Exception:
                    logger.exception("[match] set_match_score raised (draft)")
                try:
                    rounds = compute_round_breakdown(
                        summary, team_a_uid_by_puuid, team_b_uid_by_puuid
                    )
                    if rounds:
                        await asyncio.to_thread(
                            repository.set_match_rounds,
                            self.db,
                            match_doc["_id"],
                            rounds,
                        )
                except Exception:
                    logger.exception("[match] set_match_rounds raised (draft)")
                try:
                    await self._post_match_scoreboard(
                        guild,
                        summary,
                        team_a_uid_by_puuid,
                        team_b_uid_by_puuid,
                        match_doc,
                        SimpleNamespace(changes=[]),
                    )
                except Exception:
                    logger.exception("[match] _post_match_scoreboard raised (draft)")
            return

        # ELO can be weighted by Rating 2.0 when Henrik data is available.
        # Build the per-player ratings from the Henrik summary (already
        # fetched above); matches without Henrik data stay flat ±20.
        ratings = None
        if fetched is not None:
            summary_for_ratings, ta_map, tb_map = fetched
            ratings = ratings_by_uid(summary_for_ratings, {**(ta_map or {}), **(tb_map or {})})

        try:
            outcome = await asyncio.to_thread(
                apply_match_validation,
                self.db,
                match_doc,
                ratings=ratings,
            )
        except Exception:
            logger.exception("[match] apply_match_validation raised")
            # Roll back the claim to allow a retry on the next tick.
            await asyncio.to_thread(
                repository.release_elo_claim,
                self.db,
                match_doc["_id"],
            )
            return

        await asyncio.to_thread(
            repository.set_match_henrik_verified,
            self.db,
            match_doc["_id"],
            found=(fetched is not None),
            multipliers=None,
        )

        # Persist per-player ELO deltas on the match doc (all queues) so the
        # stats website can show each player's ELO change for the match.
        try:
            await asyncio.to_thread(
                repository.set_match_elo_results,
                self.db,
                match_doc["_id"],
                build_elo_results(outcome),
            )
        except Exception:
            logger.exception("[match] set_match_elo_results raised")

        embed = build_elo_changes_embed(outcome, match_doc, guild.name)
        elo_log = discord.utils.get(guild.text_channels, name="elo-adding")
        if elo_log is not None:
            try:
                await elo_log.send(embed=embed)
            except Exception:
                logger.exception("[match] ELO recap send raised")

        try:
            await refresh_leaderboard_channel(guild, self.db, queue_type)
        except Exception:
            logger.exception("[match] leaderboard refresh raised")

        # Scoreboard + extended stats: only if Henrik responded.
        if fetched is not None:
            summary, team_a_uid_by_puuid, team_b_uid_by_puuid = fetched
            # Persist the final round score (team_a vs team_b) on the match
            # doc — only available when Henrik returned the custom.
            try:
                score_a, score_b = compute_team_scores(
                    summary, team_a_uid_by_puuid, team_b_uid_by_puuid
                )
                if score_a is not None and score_b is not None:
                    await asyncio.to_thread(
                        repository.set_match_score,
                        self.db,
                        match_doc["_id"],
                        score_a,
                        score_b,
                    )
            except Exception:
                logger.exception("[match] set_match_score raised")
            # Persist the per-round breakdown (winner a/b + end type) so the
            # website can render the round bar like this scoreboard.
            try:
                rounds = compute_round_breakdown(summary, team_a_uid_by_puuid, team_b_uid_by_puuid)
                if rounds:
                    await asyncio.to_thread(
                        repository.set_match_rounds,
                        self.db,
                        match_doc["_id"],
                        rounds,
                    )
            except Exception:
                logger.exception("[match] set_match_rounds raised")
            try:
                await self._post_match_scoreboard(
                    guild,
                    summary,
                    team_a_uid_by_puuid,
                    team_b_uid_by_puuid,
                    match_doc,
                    outcome,
                )
            except Exception:
                logger.exception("[match] _post_match_scoreboard raised")
            try:
                puuid_to_user_id: dict = {}
                puuid_to_user_id.update(team_a_uid_by_puuid or {})
                puuid_to_user_id.update(team_b_uid_by_puuid or {})
                extended = build_extended_stats(
                    summary,
                    puuid_to_user_id=puuid_to_user_id,
                    queue_type=match_doc.get("queue_type", "open"),
                )
                await self._persist_extended_stats(
                    match_id=str(match_doc["_id"]),
                    extended=extended,
                )
            except Exception:
                logger.exception("[match] _persist_extended_stats wrapper raised")

    async def _persist_extended_stats(
        self,
        *,
        match_id: str,
        extended,
    ) -> None:
        """Persist per-match Rating 2.0 stats + update aggregates.

        Best-effort: errors are logged but do NOT propagate (the ELO
        is already applied at this point — the match doc is the source
        of truth for ranking).
        """
        if not extended:
            return
        now = datetime.now(UTC)

        match_docs: list[dict] = []
        deltas: list[dict] = []
        for s in extended:
            match_docs.append(
                {
                    "_id": f"{match_id}:{s.user_id}",
                    "match_id": match_id,
                    "user_id": s.user_id,
                    "queue_type": s.queue_type,
                    "map": s.map_name,
                    "agent": s.agent,
                    "rounds_played": s.rounds_played,
                    "win": s.win,
                    "kills": s.kills,
                    "deaths": s.deaths,
                    "assists": s.assists,
                    "damage_made": s.damage_made,
                    "damage_received": s.damage_received,
                    "headshots": s.headshots,
                    "bodyshots": s.bodyshots,
                    "legshots": s.legshots,
                    "multikills_2k": s.multikills_2k,
                    "multikills_3k": s.multikills_3k,
                    "multikills_4k": s.multikills_4k,
                    "multikills_5k": s.multikills_5k,
                    "first_kills": s.first_kills,
                    "first_deaths": s.first_deaths,
                    "kast_rounds": s.kast_rounds,
                    "acs": s.acs,
                    "rating_2_0": s.rating_2_0,
                    "created_at": now,
                }
            )
            deltas.append(
                {
                    "user_id": s.user_id,
                    "queue_type": s.queue_type,
                    "games": 1,
                    "rounds_played": s.rounds_played,
                    "kills": s.kills,
                    "deaths": s.deaths,
                    "assists": s.assists,
                    "damage_made": s.damage_made,
                    "damage_received": s.damage_received,
                    "headshots": s.headshots,
                    "bodyshots": s.bodyshots,
                    "legshots": s.legshots,
                    "multikills_2k": s.multikills_2k,
                    "multikills_3k": s.multikills_3k,
                    "multikills_4k": s.multikills_4k,
                    "multikills_5k": s.multikills_5k,
                    "first_kills": s.first_kills,
                    "first_deaths": s.first_deaths,
                    "kast_rounds": s.kast_rounds,
                    "rating_2_0_sum": s.rating_2_0,
                    "acs_sum": s.acs,
                    # Nombre de matchs inclus dans acs_sum : denominateur de
                    # l'ACS saison (games compte aussi les matchs d'avant
                    # l'accumulation d'ACS, qui dilueraient la moyenne).
                    "acs_games": 1,
                }
            )

        try:
            inserted = await asyncio.to_thread(
                repository.insert_match_player_stats, self.db, match_docs
            )
        except Exception:
            logger.exception("[stats] insert_match_player_stats failed")
            return

        if inserted == 0:
            return

        try:
            await asyncio.to_thread(repository.update_rating_aggregates, self.db, deltas)
        except Exception:
            logger.exception("[stats] update_rating_aggregates failed")

    async def _fetch_henrik_match_summary(
        self,
        guild,
        match_doc: dict,
    ) -> tuple[Any, dict[str, str], dict[str, str]] | None:
        """Attempt to find the HenrikDev custom for `match_doc`. Returns
        (summary, team_a_uid_by_puuid, team_b_uid_by_puuid) or None if not
        usable. The team maps let the caller decide which Henrik side
        (Red/Blue) corresponds to Team A vs Team B.

        Best-effort: a None return means "no scoreboard for this match"
        (Henrik down, custom not found, riot accounts missing, circuit
        open). The flat ELO has already been applied upstream."""

        # 10 Riot lookups (the leader is one of the 10 randomly chosen
        # players, we grab them in passing). Grouped in a single thread
        # to avoid freezing the event loop for ~10x10ms.
        def _gather_riot_accounts() -> tuple[
            Mapping[str, Any] | None, dict[str, str], dict[str, str]
        ]:
            leader_uid_local = str(match_doc.get("lobby_leader_id"))
            leader: Mapping[str, Any] | None = None
            a_map: dict[str, str] = {}
            b_map: dict[str, str] = {}
            for player in match_doc.get("team_a", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, pid)
                if r and r.get("puuid"):
                    a_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            for player in match_doc.get("team_b", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, pid)
                if r and r.get("puuid"):
                    b_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            # Fallback: if the leader is no longer one of the 10 (after
            # a /match-replace for example), do a direct lookup.
            if leader is None:
                leader = repository.get_riot_account(self.db, leader_uid_local)
            return leader, a_map, b_map

        leader_riot, team_a_uid_by_puuid, team_b_uid_by_puuid = await asyncio.to_thread(
            _gather_riot_accounts,
        )
        if not leader_riot:
            return None

        expected = set(team_a_uid_by_puuid) | set(team_b_uid_by_puuid)
        if len(expected) < 10:
            return None

        after = match_doc.get("created_at") or match_doc.get("validated_at")

        # Circuit breaker: if HenrikDev failed 3x in a row recently,
        # skip for 5 min. Without this guard, each tick (1 min)
        # would re-run N stale matches × 12s of retries each, freezing
        # the ThreadPoolExecutor and overlapping ticks.
        now = datetime.now(UTC)
        async with self._henrik_lock:
            circuit_open = (
                self._henrik_circuit_open_until is not None
                and now < self._henrik_circuit_open_until
            )
        if circuit_open:
            return None

        # `find_henrik_custom_match` makes a synchronous HTTP call (`requests`).
        # We run it in a thread to avoid blocking the Discord event loop
        # during the timeout (up to 10s per call).
        try:
            summary = await asyncio.to_thread(
                find_henrik_custom_match,
                self.henrik_client,
                region=str(leader_riot.get("riot_region", "eu")),
                leader_name=str(leader_riot.get("riot_name", "")),
                leader_tag=str(leader_riot.get("riot_tag", "")),
                expected_puuids=expected,
                after=after,
            )
        except Exception as e:
            async with self._henrik_lock:
                self._henrik_consecutive_failures += 1
                failures = self._henrik_consecutive_failures
                if failures >= HENRIK_CIRCUIT_FAIL_THRESHOLD:
                    self._henrik_circuit_open_until = now + timedelta(
                        minutes=HENRIK_CIRCUIT_OPEN_MINUTES,
                    )
                    just_opened = True
                else:
                    just_opened = False
            if just_opened:
                logger.warning(
                    "[match] Henrik circuit OPEN after %d consecutive failures. "
                    "Resuming in %d min. Last error: %r",
                    failures,
                    HENRIK_CIRCUIT_OPEN_MINUTES,
                    e,
                )
            else:
                logger.error(
                    "[match] Henrik failure (%d/%d): %r",
                    failures,
                    HENRIK_CIRCUIT_FAIL_THRESHOLD,
                    e,
                    exc_info=True,
                )
            return None
        # Success: reset the failure counter and close the circuit.
        async with self._henrik_lock:
            if self._henrik_consecutive_failures > 0 or self._henrik_circuit_open_until is not None:
                self._henrik_consecutive_failures = 0
                self._henrik_circuit_open_until = None
        if summary is None:
            return None
        return summary, team_a_uid_by_puuid, team_b_uid_by_puuid

    async def _post_match_scoreboard(
        self,
        guild,
        summary,
        team_a_uid_by_puuid: dict[str, str],
        team_b_uid_by_puuid: dict[str, str],
        match_doc: dict,
        outcome,
    ) -> None:
        """Best-effort: build the per-match scoreboard image and post it
        in the queue's results channel (one per queue_type, e.g.
        open-results / advanced-results). Silently no-op if the channel is
        missing or the image generation fails — the flat ELO was already
        applied upstream and shouldn't be blocked by a cosmetic post."""
        queue_type = match_doc.get("queue_type", "open")
        channel_name = RESULTS_CHANNELS.get(queue_type)
        if not channel_name:
            return
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            logger.warning(
                "[match] results channel %r not found on guild %s; skipping scoreboard",
                channel_name,
                guild.id,
            )
            return

        rounds = max(int(getattr(summary, "rounds_played", 0)) or 1, 1)
        elo_by_uid = {c.user_id: c.new_elo for c in outcome.changes}
        elo_delta_by_uid = {c.user_id: c.delta for c in outcome.changes}

        # Resolve which Henrik side (Red/Blue) corresponds to each bot team.
        # Use majority vote — in the edge case of "mixed teams in the Valorant
        # lobby" both teams may share a side, but majority gives a reasonable
        # rendering anyway.
        def _side(uid_by_puuid: dict[str, str]) -> str:
            counts = {"Red": 0, "Blue": 0}
            for p in summary.players:
                if p.puuid in uid_by_puuid:
                    counts[p.team] = counts.get(p.team, 0) + 1
            return "Red" if counts["Red"] >= counts["Blue"] else "Blue"

        a_side = _side(team_a_uid_by_puuid)
        b_side = "Blue" if a_side == "Red" else "Red"
        rounds_a = summary.rounds_red if a_side == "Red" else summary.rounds_blue
        rounds_b = summary.rounds_red if b_side == "Red" else summary.rounds_blue

        def _rows(uid_by_puuid: dict[str, str]) -> list[dict]:
            rows = []
            for stats in summary.players:
                uid = uid_by_puuid.get(stats.puuid)
                if uid is None:
                    continue
                member = guild.get_member(int(uid)) if uid.isdigit() else None
                # Prefer Discord display name; fall back to Riot name#tag
                # if the user is no longer on the server (kick/leave).
                if member is not None:
                    display_name = member.display_name
                else:
                    display_name = f"{stats.name}#{stats.tag}" if stats.tag else stats.name
                shots_total = (
                    int(getattr(stats, "headshots", 0) or 0)
                    + int(getattr(stats, "bodyshots", 0) or 0)
                    + int(getattr(stats, "legshots", 0) or 0)
                )
                hs_pct = (
                    (int(getattr(stats, "headshots", 0) or 0) / shots_total) * 100.0
                    if shots_total
                    else 0.0
                )
                kast_pct = (int(getattr(stats, "kast_rounds", 0) or 0) / rounds) * 100.0
                adr = int(getattr(stats, "damage_made", 0) or 0) / rounds
                rating_2_0 = compute_rating_2_0(
                    RatingInputs(
                        rounds_played=rounds,
                        kills=int(stats.kills or 0),
                        deaths=int(stats.deaths or 0),
                        assists=int(stats.assists or 0),
                        damage_made=int(getattr(stats, "damage_made", 0) or 0),
                        kast_rounds=int(getattr(stats, "kast_rounds", 0) or 0),
                    )
                )
                rows.append(
                    {
                        "name": display_name,
                        "kills": stats.kills,
                        "deaths": stats.deaths,
                        "assists": stats.assists,
                        "acs": round(stats.score / rounds),
                        "elo": elo_by_uid.get(uid, 0),
                        "elo_delta": elo_delta_by_uid.get(uid, 0),
                        "agent": getattr(stats, "agent", ""),
                        "rating_2_0": rating_2_0,
                        "kast_pct": kast_pct,
                        "adr": adr,
                        "hs_pct": hs_pct,
                        "first_kills": int(getattr(stats, "first_kills", 0) or 0),
                        "first_deaths": int(getattr(stats, "first_deaths", 0) or 0),
                    }
                )
            return rows

        team_a_rows = _rows(team_a_uid_by_puuid)
        team_b_rows = _rows(team_b_uid_by_puuid)
        queue_label = _QUEUE_LABEL_BY_TYPE.get(queue_type, queue_type)
        # Re-order the per-round winners so that index 0 maps to team A's
        # actual perspective. Henrik labels each round by Red/Blue, but
        # team A may be either; the scoreboard expects A = Red.
        round_winners = tuple(getattr(summary, "round_winners", ()) or ())
        if a_side == "Blue":
            round_winners = tuple(
                "Red" if w == "Blue" else ("Blue" if w == "Red" else "") for w in round_winners
            )
        # End-type is symmetric per round, no remap needed.
        round_end_types = tuple(getattr(summary, "round_end_types", ()) or ())

        try:
            buf = await asyncio.to_thread(
                generate_scoreboard,
                map_name=summary.map_name,
                rounds_a=rounds_a,
                rounds_b=rounds_b,
                team_a_label="Team A",
                team_b_label="Team B",
                team_a_players=team_a_rows,
                team_b_players=team_b_rows,
                queue_label=queue_label,
                round_winners=round_winners,
                round_end_types=round_end_types,
            )
        except Exception:
            logger.exception("[match] scoreboard image generation raised")
            return

        try:
            await channel.send(file=discord.File(buf, filename=f"scoreboard_{queue_type}.png"))
        except Exception:
            logger.exception("[match] scoreboard send raised")
