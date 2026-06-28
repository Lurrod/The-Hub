"""Match formation flow for MatchCog (queue-full -> match created)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import discord

from cogs.match._base import MatchCogState
from cogs.match._constants import (
    _QUEUE_PREFIX_BY_TYPE,
    ADMIN_ROLE_NAMES,
    MATCH_HOST_ROLE_NAME,
    MATCH_HUB_SPECTATOR_ROLE_NAMES,
    MATCH_SPECTATOR_ROLE_NAMES,
    MATCH_VIEWER_ROLE_NAMES,
)
from cogs.match._embeds import (
    build_match_embed,
)
from cogs.queue_v2 import (
    QUEUE_CHANNEL_NAMES,
    QUEUE_ROLE_NAME,
)
from services import elo_calc, repository
from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
    pick_captains,
)
from services.map_pick_ban import (
    MapBanCancelledError,
    MapBanSession,
)
from services.match_category import (
    create_match_category,
    delete_match_category,
)
from services.match_service import (
    MatchPlan,
    build_plan_from_draft,
    build_players,
    plan_match,
    serialize_team,
)
from services.repository import reserve_match_number
from services.team_balancer import Player

logger = logging.getLogger(__name__)


class FormationMixin(MatchCogState):
    async def on_queue_full(
        self,
        interaction: discord.Interaction,
        queue_doc: dict,
        queue_type: str = "open",
    ):
        guild = interaction.guild
        players = await self._resolve_match_players(interaction, queue_doc, queue_type)
        if players is None:
            return None
        player_ids = [str(p.id) for p in players]

        # Origin channel of the queue (to repost setup-queue afterwards).
        queue_channel = guild.get_channel(int(queue_doc["channel_id"]))
        if queue_channel is None:
            await self._fail(
                interaction,
                queue_doc,
                "Salon de la file introuvable.",
                queue_type=queue_type,
            )
            return None

        # Reserve an atomic match number + dynamically create the Discord category.
        match_number = await asyncio.to_thread(reserve_match_number, self.db, guild_id=guild.id)
        try:
            channels = await create_match_category(
                guild=guild,
                match_number=match_number,
                player_ids=[p.id for p in players],
                admin_role_ids=await asyncio.to_thread(self._admin_role_ids, guild),
                viewer_role_ids=self._viewer_role_ids(guild),
                spectator_role_ids=self._spectator_role_ids(guild),
                hub_spectator_role_ids=self._hub_spectator_role_ids(guild),
                team_prefix=_QUEUE_PREFIX_BY_TYPE.get(queue_type, ""),
            )
        except Exception:
            logger.exception("[match] create_match_category failed for #%d", match_number)
            await interaction.followup.send(
                "Erreur Discord lors de la création de la catégorie du match. Réessaye.",
                ephemeral=True,
            )
            return None
        category = channels.category
        prep_channel = channels.prep_channel
        free_cat_name = category.name

        # Persist a 'preparing' placeholder doc BEFORE draft/map-ban so:
        # - admins can /match-cancel during draft/ban (DB lookup by channel)
        # - startup orphan cleanup keeps the category (status is active)
        # - startup recovery detects bot restarts mid-draft and cleans up
        preparing_match_id = await asyncio.to_thread(
            repository.create_preparing_match,
            self.db,
            queue_type=queue_type,
            origin_guild_id=guild.id,
            match_number=match_number,
            category_id=category.id,
            channel_id=prep_channel.id,
            player_ids=[int(p.id) for p in players],
        )

        # Advanced & Draft: captain draft + map ban. Open: auto-balance (by
        # peak ELO over the last 6 months) + random map.
        if queue_type in ("advanced", "draft"):
            plan = await self._run_pro_draft_and_ban(
                interaction,
                guild,
                prep_channel,
                players,
                match_number=match_number,
                category=category,
                preparing_match_id=preparing_match_id,
                free_cat_name=free_cat_name,
            )
            if plan is None:
                return None
        else:
            plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

        # Setup ordering: the match doc was already inserted with
        # status='preparing' before captain draft / map ban started, so
        # the channel is always resolvable from DB. Here we promote it
        # to 'pending' with the now-known teams/map (message_id is
        # filled in later, after the announcement is sent).
        match_id = preparing_match_id
        await asyncio.to_thread(
            repository.finalize_preparing_match,
            self.db,
            match_id,
            team_a=serialize_team(plan.teams.team_a),
            team_b=serialize_team(plan.teams.team_b),
            map_name=plan.map_name,
            lobby_leader_id=plan.lobby_leader.id,
            category_name=plan.category_name,
            team_a_side=plan.team_a_side,
        )

        # Step 2: adjust roles BEFORE announcing.
        await self._setup_match_roles(guild, player_ids, plan)

        # Step 3: send the announcement.
        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed = build_match_embed(plan, guild.name, queue_type)
        try:
            msg = await prep_channel.send(
                content=f"🎯 Match trouvé ! {mentions}",
                embed=embed,
                view=self.vote_view,
            )
        except Exception:
            # The announcement failed: cancel the freshly created match
            # doc to avoid an orphan that nobody can vote on (no
            # message_id => VoteView cannot be found).
            logger.exception("[match] prep_channel.send raised, rolling back match doc")
            matches_col = repository.get_matches_col(self.db)
            await asyncio.to_thread(
                matches_col.delete_one,
                {"_id": match_id},
            )
            await self._fail(
                interaction,
                queue_doc,
                "Échec de l'envoi de l'annonce du match. Match annulé.",
                queue_type=queue_type,
            )
            return None

        # Step 4: associate the message_id with the match doc. Without
        # this, `get_match_by_message` (used by VoteView) cannot find
        # the match at vote time.
        matches_col = repository.get_matches_col(self.db)
        await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match_id},
            {"$set": {"message_id": msg.id}},
        )

        # Step 5: empty the queue immediately after persistence.
        # Prevents any potential re-trigger of on_queue_full on the same queue.
        await asyncio.to_thread(
            repository.delete_active_queue,
            self.db,
            guild.id,
            queue_type,
        )

        # Step 5b: strip the Join/Leave buttons off the now-stale queue
        # message. A single QueueView instance is shared across every
        # message of this queue_type (the custom_id is keyed on queue_type,
        # not on the message). Once the match is formed and a fresh queue is
        # reposted (Step 7), clicking Join/Leave on this old "match found"
        # message would otherwise mutate the new queue and overwrite this
        # message's content. Removing its view makes it inert. Best-effort:
        # a missing/deleted message must not abort match formation.
        old_msg_id = queue_doc.get("message_id")
        if old_msg_id is not None:
            try:
                old_msg = await queue_channel.fetch_message(int(old_msg_id))
                await old_msg.edit(view=None)
            except Exception:
                logger.debug(
                    "[match] could not strip buttons off old queue message %s",
                    old_msg_id,
                    exc_info=True,
                )

        # Step 6: voice move Waiting Room -> Team 1/Team 2 based on
        # the assignment computed by balance_teams. Players land
        # directly in their team VC, no need to re-split after the
        # Waiting Match gathering.
        await self._move_players_to_match_vc(guild, free_cat_name, plan)

        # Step 7: repost setup-queue (best-effort) in the destination
        # channel for this queue_type. We preserve the origin channel
        # (queue_doc.channel_id) if possible, otherwise we fall back on
        # the channel named QUEUE_CHANNEL_NAMES[queue_type].
        target_channel = queue_channel
        target_name = QUEUE_CHANNEL_NAMES.get(queue_type)
        if target_name and target_channel.name != target_name:
            named = discord.utils.get(guild.text_channels, name=target_name)
            if named is not None:
                target_channel = named
        queue_cog = self.bot.get_cog("QueueCog")
        if queue_cog is not None:
            try:
                await queue_cog.post_queue_message(target_channel, queue_type)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("[match] failed to re-post setup-queue")
        return match_id

    async def _resolve_match_players(
        self,
        interaction: discord.Interaction,
        queue_doc: dict,
        queue_type: str,
    ) -> list[Player] | None:
        """Batch-load Riot accounts + ELO for the queued players and build
        the Player list. Returns None (after notifying via ``_fail``) when a
        player has no linked Riot account.

        Batches 2 Mongo queries instead of 20 (N+1): the 10 Riot accounts
        and the 10 ELO docs in a single query each, grouped in one thread
        to avoid freezing the event loop during match formation.
        """
        guild = interaction.guild
        player_ids = [str(uid) for uid in queue_doc.get("players", [])]
        elo_col = repository.get_elo_col(self.db)
        riot_col = repository.get_riot_col(self.db)

        def _batch_fetch() -> tuple[dict[str, dict], dict[str, int]]:
            riot_map: dict[str, dict] = {}
            elo_map: dict[str, int] = {}
            for doc in riot_col.find({"_id": {"$in": player_ids}}):
                riot_map[str(doc["_id"])] = dict(doc)
            # Compound _id: map of "uid:queue_type" -> elo. We store by
            # bare uid so that `build_players` stays pure (bare uid key).
            compound_ids = [repository.player_doc_id(uid, queue_type) for uid in player_ids]
            for doc in elo_col.find({"_id": {"$in": compound_ids}}):
                uid = str(doc["_id"]).split(":", 1)[0]
                elo_map[uid] = int(doc.get("elo", elo_calc.ELO_START))
            return riot_map, elo_map

        riot_accounts, bot_elos = await asyncio.to_thread(_batch_fetch)

        # Players without an ELO doc yet (first match, or post-reset):
        # default to ELO_START instead of 0. `build_players` reads these
        # via `bot_elos.get(uid, 0)`; we therefore fill the fallback
        # explicitly here to keep `build_players` pure.
        for uid in player_ids:
            bot_elos.setdefault(uid, elo_calc.ELO_START)

        # Open queue: balance teams on the player's PEAK ELO over the last
        # 6 months (computed at /link-riot and stored on the Riot doc),
        # not the accumulated server ELO. Fallback to the server ELO /
        # ELO_START when the peak is unknown (peak_elo <= 0).
        if queue_type == "open":
            for uid in player_ids:
                peak = int((riot_accounts.get(uid) or {}).get("peak_elo", 0) or 0)
                if peak > 0:
                    bot_elos[uid] = peak

        member_names: dict[str, str] = {}
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member:
                member_names[uid] = member.display_name

        players = build_players(player_ids, riot_accounts, member_names, bot_elos)
        if len(players) < 10:
            await self._fail(
                interaction,
                queue_doc,
                "Joueur(s) sans compte Riot lié. Match annulé.",
                queue_type=queue_type,
            )
            return None
        return players

    async def _run_pro_draft_and_ban(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        prep_channel: discord.TextChannel,
        players: list[Player],
        *,
        match_number: int,
        category: discord.CategoryChannel,
        preparing_match_id: Any,
        free_cat_name: str,
    ) -> MatchPlan | None:
        """Run captain draft + map ban for the advanced flow.

        Returns the match plan, or None when the draft/ban is cancelled or
        fails — in which case the preparing doc and the Discord category are
        already rolled back and the players have been notified.
        """
        player_ids_for_move = [str(p.id) for p in players]
        await self._move_players_to_waiting_match(guild, category, player_ids_for_move)
        cap_a, cap_b = pick_captains(players, rng=self.rng)
        pool = tuple(p for p in players if p.id not in (cap_a.id, cap_b.id))
        draft_session = CaptainDraftSession(
            prep_channel=prep_channel,
            cap_a=cap_a,
            cap_b=cap_b,
            pool=pool,
            admin_role_names=ADMIN_ROLE_NAMES,
        )
        try:
            draft_result = await draft_session.run()
        except DraftCancelledError as exc:
            logger.info(
                "[match] draft cancelled (reason=%s actor=%s) - queue preserved",
                exc.reason,
                getattr(exc.actor, "id", None),
            )
            await asyncio.to_thread(repository.cancel_preparing_match, self.db, preparing_match_id)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "❌ Draft annulé. La file reste active. "
                    "`/leave` puis `/join` pour réinitialiser si besoin.",
                    ephemeral=False,
                )
            try:
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} draft annulé",
                )
            except Exception:
                logger.exception("[match] failed to delete category on draft cancel")
            return None
        except Exception:
            logger.exception(
                "[match] captain draft failed for #%d, rolling back category",
                match_number,
            )
            await asyncio.to_thread(repository.cancel_preparing_match, self.db, preparing_match_id)
            await delete_match_category(
                guild=guild,
                category_id=category.id,
                reason=f"Match #{match_number} draft interrompu",
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    f"❌ Le draft du Match #{match_number} a échoué, match annulé.",
                    ephemeral=True,
                )
            return None

        ban_session = MapBanSession(
            prep_channel=prep_channel,
            cap_a=cap_a,
            cap_b=cap_b,
            maps=elo_calc.MAPS,
            admin_role_names=ADMIN_ROLE_NAMES,
        )
        try:
            ban_result = await ban_session.run()
        except MapBanCancelledError as exc:
            logger.info(
                "[match] map ban cancelled (reason=%s actor=%s) - queue preserved",
                exc.reason,
                getattr(exc.actor, "id", None),
            )
            await asyncio.to_thread(repository.cancel_preparing_match, self.db, preparing_match_id)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "❌ Ban de carte annulé. La file reste active. "
                    "`/leave` puis `/join` pour réinitialiser si besoin.",
                    ephemeral=False,
                )
            try:
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} ban de carte annulé",
                )
            except Exception:
                logger.exception("[match] failed to delete category on map ban cancel")
            return None
        except Exception:
            logger.exception(
                "[match] map ban failed for #%d, rolling back category",
                match_number,
            )
            await asyncio.to_thread(repository.cancel_preparing_match, self.db, preparing_match_id)
            await delete_match_category(
                guild=guild,
                category_id=category.id,
                reason=f"Match #{match_number} ban de carte interrompu",
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    f"❌ Le ban de carte du Match #{match_number} a échoué, match annulé.",
                    ephemeral=True,
                )
            return None

        return build_plan_from_draft(
            draft_result,
            free_category=free_cat_name,
            rng=self.rng,
            map_name=ban_result.selected_map,
            team_a_side=ban_result.picked_side,
        )

    async def _setup_match_roles(
        self,
        guild: discord.Guild,
        player_ids: list[str],
        plan: MatchPlan,
    ) -> None:
        """Best-effort role setup before announcing: strip the queue role
        from every player and grant the match-host role to the lobby leader.

        Best-effort: a crash here leaves partial roles but the match doc
        exists -> /match-cancel cleans up. Consolidated to 1 PATCH/player
        via ``member.edit(roles=...)`` (atomic diff on Discord's side) to
        eliminate the 429s observed in prod (per-guild PATCH /members/{u}
        bucket ~10/10s). Semaphore(5) as a safeguard.
        """
        leader_id = int(plan.lobby_leader.id)

        async def _setup_roles_for(member: discord.Member) -> None:
            mg = member.guild
            queue_role = discord.utils.get(mg.roles, name=QUEUE_ROLE_NAME)
            host_role = (
                discord.utils.get(mg.roles, name=MATCH_HOST_ROLE_NAME)
                if member.id == leader_id
                else None
            )
            current = set(member.roles)
            target = set(current)
            if queue_role is not None:
                target.discard(queue_role)
            if host_role is not None:
                target.add(host_role)
            if target == current:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.edit(
                        roles=list(target),
                        reason="Match formé : configuration des rôles",
                    )

        role_members = [
            m for m in (guild.get_member(int(uid)) for uid in player_ids) if m is not None
        ]
        if not any(m.id == leader_id for m in role_members):
            leader_member = guild.get_member(leader_id)
            if leader_member is not None:
                role_members.append(leader_member)
        role_results = await asyncio.gather(
            *(_setup_roles_for(m) for m in role_members),
            return_exceptions=True,
        )
        for r in role_results:
            if isinstance(r, BaseException):
                logger.warning("[match] role setup failed: %r", r)

    def _admin_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of the admin/staff roles to include in the
        overwrites of the match category.

        Covers two sources:
          1. The roles named in `ADMIN_ROLE_NAMES` (project constant):
             allows custom moderators without Discord `administrator`
             permission to view/manage the dynamic match categories.
          2. The bypass role configured via /bypass (`bypass` collection
             in the DB, per guild). Used by servers that have a custom
             moderation role not listed in ADMIN_ROLE_NAMES.

        Without this method wired up, only users with the Discord
        `administrator` permission (which bypasses overwrites) see the
        categories -- which excludes custom staff.
        """
        # Manual iteration (not `discord.utils.get`): on mocked Guilds
        # in tests, `utils.get` may return a coroutine via the `_aget`
        # fallback which does not expose `.id`.
        admin_names: set[str] = set(ADMIN_ROLE_NAMES)
        ids: list[int] = []
        try:
            roles_iter = list(guild.roles)
        except TypeError:
            roles_iter = []
        for role in roles_iter:
            name = getattr(role, "name", None)
            role_id = getattr(role, "id", None)
            if isinstance(name, str) and name in admin_names and isinstance(role_id, int):
                ids.append(role_id)
        try:
            bypass_id = repository.get_bypass_role(self.db, guild.id)
        except Exception:  # pragma: no cover - guild.id missing/mock weirdness
            bypass_id = None
        if isinstance(bypass_id, int) and bypass_id not in ids:
            ids.append(bypass_id)
        return ids

    def _viewer_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of the "viewer" staff roles to include in the
        overwrites of the match category (player-level access, not admin).

        These roles (see MATCH_VIEWER_ROLE_NAMES) receive the same rights
        as the 10 players: view/send/connect/speak, without manage_channels.
        Useful so that staff (Head Administrators, Administrators, THE HUB)
        can follow/help on any match category without having admin powers
        (draft cancel, ping, channel management).
        """
        return self._role_ids_by_names(guild, MATCH_VIEWER_ROLE_NAMES)

    def _spectator_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of "spectator" roles (see MATCH_SPECTATOR_ROLE_NAMES,
        e.g. "Members"): they see the category + read history, but cannot
        join the voice channels or send messages.
        """
        return self._role_ids_by_names(guild, MATCH_SPECTATOR_ROLE_NAMES)

    def _hub_spectator_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of "hub spectator" roles (see
        MATCH_HUB_SPECTATOR_ROLE_NAMES, e.g. "FL HUB"): they see the
        match category and voice channels in the sidebar but cannot
        join voice nor read the prep text channel — that channel is
        hidden via a per-channel view_channel=False override. Players
        in the match keep full access via their member-level overwrite.
        """
        return self._role_ids_by_names(guild, MATCH_HUB_SPECTATOR_ROLE_NAMES)

    @staticmethod
    def _role_ids_by_names(guild: discord.Guild, names: tuple[str, ...]) -> list[int]:
        wanted: set[str] = set(names)
        ids: list[int] = []
        try:
            roles_iter = list(guild.roles)
        except TypeError:
            roles_iter = []
        for role in roles_iter:
            name = getattr(role, "name", None)
            role_id = getattr(role, "id", None)
            if isinstance(name, str) and name in wanted and isinstance(role_id, int):
                ids.append(role_id)
        return ids

    async def _move_players_to_match_vc(
        self,
        guild,
        free_cat_name: str,
        plan,
    ) -> None:
        """Move the 10 players into the team VC (`Team 1` / `Team 2`)
        of the assigned category, according to `plan.teams.team_a` /
        `team_b`. Silently skip players who are out of voice or already
        in place.

        Graceful fallback if a team VC is missing: fall back to the
        other one if available, otherwise to `Waiting Match`, otherwise
        no-op. All valid players have already been auto-moved into
        `Waiting Room` on clicking Join (see queue_v2._move_to_waiting_room).
        """
        category = discord.utils.get(guild.categories, name=free_cat_name)
        if category is None:
            return
        team1_vc = discord.utils.get(category.voice_channels, name="Team 1")
        team2_vc = discord.utils.get(category.voice_channels, name="Team 2")
        waiting_match = discord.utils.get(
            category.voice_channels,
            name="Waiting Match",
        )

        # uid -> target VC mapping. team_a -> Team 1, team_b -> Team 2.
        # If a team VC is missing, fall back to the other one then to
        # Waiting Match to guarantee the player is regrouped even in
        # degraded config.
        a_dest = team1_vc or team2_vc or waiting_match
        b_dest = team2_vc or team1_vc or waiting_match
        if a_dest is None and b_dest is None:
            return

        targets: dict[int, Any] = {}
        for player in plan.teams.team_a:
            if a_dest is not None:
                targets[int(player.id)] = a_dest
        for player in plan.teams.team_b:
            if b_dest is not None:
                targets[int(player.id)] = b_dest

        # Parallelization: per-member bucket, but we cap at 5 concurrent
        # via the semaphore shared with role edits so we never saturate
        # the Discord PATCH /members/{u} per-guild bucket (~10/10s).
        async def _move_one(uid: int, dest) -> None:
            member = guild.get_member(uid)
            if member is None:
                return
            voice = getattr(member, "voice", None)
            if voice is None or getattr(voice, "channel", None) is None:
                return
            if voice.channel.id == dest.id:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.move_to(
                        dest,
                        reason="Match formé : regroupement dans le vocal d'équipe",
                    )

        await asyncio.gather(
            *(_move_one(uid, dest) for uid, dest in targets.items()),
            return_exceptions=True,
        )

    async def _move_players_to_waiting_match(
        self,
        guild,
        category,
        player_ids: list[str],
    ) -> None:
        """Move all `player_ids` to the 'Waiting Match' VC of `category`.

        Used on the advanced branch BEFORE the captain draft, so the
        10 players are grouped in one VC while captains pick their teams.

        Guards:
          - skip if guild.get_member returns None
          - skip if member is not in voice
          - skip if already at destination
        """
        waiting_match = discord.utils.get(category.voice_channels, name="Waiting Match")
        if waiting_match is None:
            logger.warning(
                "[match] _move_players_to_waiting_match: 'Waiting Match' not found in %s, no-op",
                category.name,
            )
            return

        async def _move_one(uid_str: str) -> None:
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                return
            member = guild.get_member(uid)
            if member is None:
                return
            voice = getattr(member, "voice", None)
            if voice is None or voice.channel is None:
                return
            if voice.channel.id == waiting_match.id:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.move_to(
                        waiting_match,
                        reason="File advanced : regroupement avant le draft des capitaines",
                    )

        await asyncio.gather(
            *[_move_one(uid) for uid in player_ids],
            return_exceptions=True,
        )

    async def _fail(
        self,
        interaction,
        queue_doc,
        reason: str,
        queue_type: str = "open",
    ) -> None:
        await asyncio.to_thread(
            repository.delete_active_queue,
            self.db,
            interaction.guild.id,
            queue_type,
        )
        channel = None
        try:
            channel = interaction.guild.get_channel(int(queue_doc["channel_id"]))
            if channel:
                await channel.send(
                    f"⚠️ {reason} Une nouvelle file a été republiée.",
                )
        except Exception:
            logger.exception("[match] _fail send raised")
        # Repost a fresh queue to avoid forcing the admin to redo
        # /setup-queue manually after every formation failure.
        if channel is not None:
            queue_cog = self.bot.get_cog("QueueCog")
            if queue_cog is not None:
                try:
                    await queue_cog.post_queue_message(channel, queue_type)  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("[match] _fail re-post queue raised")
