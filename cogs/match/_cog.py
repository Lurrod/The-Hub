"""MatchCog - orchestrator of the match flow.

Remains a large cog (~1300 lines) because the match transitions (formation,
vote, Henrik verification, cleanups) share `self` state (db,
henrik_client, circuit breaker, role-edit semaphore). Splitting into
multiple mixins would add reverse coupling without gaining readability.

Splitting the *module* into sub-files (`_constants`, `_embeds`,
`_vote`) does however extract the purely functional blocks from the cog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from bson import ObjectId
from bson.errors import InvalidId
from discord import app_commands
from discord.ext import commands, tasks

from cogs.match._constants import (
    CONTESTED_EXPIRY_HOURS,
    MATCH_HOST_ROLE_NAME,
    MAX_REPLACE_ELO_DIFF,
)
from cogs.match._formation import FormationMixin
from cogs.match._lifecycle import LifecycleMixin
from cogs.match._verification import VerificationMixin
from cogs.match._vote import VoteView
from services import elo_calc, repository
from services.match_category import (
    delete_match_category,
)
from services.riot_api import HenrikDevClient

logger = logging.getLogger(__name__)


class MatchCog(FormationMixin, VerificationMixin, LifecycleMixin, commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db,
        *,
        rng: random.Random | None = None,
        henrik_client: HenrikDevClient | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.rng = rng or random.Random()
        self.henrik_client = henrik_client
        self.vote_view = VoteView(db, on_validated=self._on_match_validated)
        # Henrik circuit breaker: suspends calls after N consecutive failures.
        # `_henrik_lock` serializes counter/open-state transitions when
        # several verifications run in parallel (asyncio.gather over guilds).
        self._henrik_consecutive_failures: int = 0
        self._henrik_circuit_open_until: datetime | None = None
        self._henrik_lock: asyncio.Lock = asyncio.Lock()
        # Safeguard for Discord rate limits on role/voice operations.
        # Discord caps the per-guild bucket (PATCH /members/{u}) at ~10/10s;
        # we cap at 5 concurrent calls to never saturate (match formation
        # = 10 simultaneous players, otherwise 429 + ~9s retry).
        self._guild_member_edit_sem: asyncio.Semaphore = asyncio.Semaphore(5)

    # ── Queue-full hook ──────────────────────────────────────────

    # ── Hook: vote validated ─────────────────────────────────────

    # ── Periodic loop (1 min) ────────────────────────────────────
    @tasks.loop(minutes=1)
    async def _timeout_loop(self):
        try:
            await self.check_vote_timeouts()
        except Exception:
            logger.exception("[match] check_vote_timeouts raised")
        try:
            await self.check_henrik_verifications()
        except Exception:
            logger.exception("[match] check_henrik_verifications raised")
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] expire_stale_contested_matches raised")

    async def expire_stale_contested_matches(self, *, now: datetime | None = None) -> int:
        """Auto-expire `contested` matches older than CONTESTED_EXPIRY_HOURS.
        Without this, an unresolved contested match (no admin action)
        freezes the 10 players in the find_active_match_for_player gate.

        Scoped per guild: avoids touching other guilds' matches.

        Returns:
            Total number of docs expired across all guilds.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=CONTESTED_EXPIRY_HOURS)
        total = 0
        for guild in self.bot.guilds:
            try:
                n = await asyncio.to_thread(
                    repository.expire_stale_contested,
                    self.db,
                    origin_guild_id=guild.id,
                    cutoff_dt=cutoff,
                )
            except Exception:
                logger.exception("[match] expire_stale_contested guild=%s raised", guild.id)
                continue
            if n:
                logger.info(
                    "[match] auto-expire contested: %d match(es) cleaned_up in guild %s",
                    n,
                    guild.name,
                )
            total += n
        return total

    @_timeout_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    @_timeout_loop.error
    async def _timeout_loop_error(self, error: BaseException) -> None:
        """Safety net: `tasks.loop` dies silently if an exception leaks
        outside the tick's internal try/except. Without this handler,
        timed-out votes would no longer be processed until the next
        bot restart."""
        # logger.error with exc_info=tuple: preserves the stack of the
        # `error` passed as argument (logger.exception() uses
        # sys.exc_info() which is not the current `error` here).
        logger.error(
            "[match] _timeout_loop raised: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            self._timeout_loop.restart()
        except Exception:
            logger.exception("[match] _timeout_loop.restart() raised")

    # ── Admin slash commands (cancel / replace) ─────────────────
    @app_commands.command(
        name="match-cancel",
        description="Annule le match en cours dans ce salon (admin)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # Atomic CAS: if a concurrent vote validates the match or if
        # _verify_match claims the ELO between read and write, the cancel
        # fails cleanly rather than creating an inconsistent state.
        match = await asyncio.to_thread(
            repository.cancel_match_atomically,
            self.db,
            channel_id=interaction.channel_id,
        )
        if not match:
            await interaction.followup.send(
                "❌ Aucun match annulable trouvé dans ce salon "
                "(statut pending/validated/contested et ELO non appliqué).",
                ephemeral=True,
            )
            return

        category_name = match.get("category_name")

        # Revoke the "Match Host" role from the lobby leader.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader = interaction.guild.get_member(int(leader_id))
            if leader is not None:
                host_role = discord.utils.get(interaction.guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader.remove_roles(host_role, reason="Match annulé")

        try:
            msg_id = match.get("message_id")
            if msg_id and interaction.channel:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=None)
        except Exception:
            logger.exception("[match-cancel] view removal raised")

        # Deletion of the match's Discord category.
        # Graceful: legacy matches without category_id are ignored.
        category_id = match.get("category_id")
        if category_id:
            await asyncio.to_thread(repository.mark_match_cleanup_started, self.db, match["_id"])
            await delete_match_category(
                guild=interaction.guild,
                category_id=category_id,
                reason=f"Match #{match.get('match_number', '?')} annulé par un admin",
            )

        await interaction.followup.send(
            f"✅ Match annulé. Catégorie `{category_name or '?'}` libérée.",
            ephemeral=True,
        )

    @app_commands.command(
        name="match-replace",
        description="Remplace un joueur dans le match en cours (admin)",
    )
    @app_commands.describe(
        leaver="Joueur à remplacer",
        replacement="Nouveau joueur (doit avoir un compte Riot lié)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_replace(
        self,
        interaction: discord.Interaction,
        leaver: discord.Member,
        replacement: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if leaver.id == replacement.id:
            await interaction.followup.send(
                "❌ Impossible de remplacer un joueur par lui-même.",
                ephemeral=True,
            )
            return

        match = await self._fetch_pending_match(interaction)
        if match is None:
            return

        team_key = self._team_of_player(match, leaver.id)
        if team_key is None:
            await interaction.followup.send(
                f"❌ {leaver.mention} ne fait pas partie de ce match.",
                ephemeral=True,
            )
            return

        if self._is_player_in_match(match, replacement.id):
            await interaction.followup.send(
                f"❌ {replacement.mention} est déjà dans ce match.",
                ephemeral=True,
            )
            return

        new_elo = await self._resolve_replacement_elo(replacement, match)
        if new_elo is None:
            await interaction.followup.send(
                f"❌ {replacement.mention} n'a pas de compte Riot lié (`/link-riot Nom#TAG`).",
                ephemeral=True,
            )
            return

        leaver_elo = self._elo_of_player(match, team_key, leaver.id)
        if not await self._replace_within_elo_band(
            interaction, leaver, replacement, leaver_elo, new_elo
        ):
            return

        leader_replaced, modified = await self._apply_replace_update(
            match, team_key, leaver.id, replacement, new_elo
        )
        if not modified:
            await interaction.followup.send(
                "❌ Le match a été validé ou annulé entre-temps. Remplacement abandonné.",
                ephemeral=True,
            )
            return

        if leader_replaced:
            await self._transfer_match_host_role(interaction.guild, leaver, replacement)

        suffix = " (hôte du lobby)" if leader_replaced else ""
        await interaction.followup.send(
            f"✅ {leaver.mention} remplacé par {replacement.mention} dans `{team_key}`{suffix}.",
            ephemeral=True,
        )

    async def _fetch_pending_match(self, interaction: discord.Interaction) -> dict | None:
        """Fetch the active ``pending`` match doc for the current channel.

        Sends an error followup and returns ``None`` if no such match exists.
        """
        matches_col = repository.get_matches_col(self.db)
        match = await asyncio.to_thread(
            matches_col.find_one,
            {"channel_id": interaction.channel_id, "status": "pending"},
        )
        if not match:
            await interaction.followup.send(
                "❌ Aucun match en cours (statut pending) dans ce salon.",
                ephemeral=True,
            )
        return match

    @staticmethod
    def _team_of_player(match: dict, user_id: int) -> str | None:
        """Return ``"team_a"`` / ``"team_b"`` for ``user_id`` or ``None``."""
        for tk in ("team_a", "team_b"):
            if any(int(p.get("id", 0)) == user_id for p in match.get(tk, [])):
                return tk
        return None

    @staticmethod
    def _is_player_in_match(match: dict, user_id: int) -> bool:
        return any(
            int(p.get("id", 0)) == user_id for tk in ("team_a", "team_b") for p in match.get(tk, [])
        )

    @staticmethod
    def _elo_of_player(match: dict, team_key: str, user_id: int) -> int:
        player = next(
            (p for p in match[team_key] if int(p.get("id", 0)) == user_id),
            None,
        )
        return int(player.get("elo", 0)) if player else 0

    async def _resolve_replacement_elo(
        self,
        replacement: discord.Member,
        match: dict,
    ) -> int | None:
        """Look up the replacement's queue-typed ELO.

        Returns ``None`` if the player has no linked Riot account; falls
        back to ``ELO_START`` if they have no ELO doc yet for this queue.
        """
        riot = await asyncio.to_thread(
            repository.get_riot_account,
            self.db,
            replacement.id,
        )
        if not riot:
            return None
        match_queue_type = match.get("queue_type", "open")
        elo_col = repository.get_elo_col(self.db)
        elo_doc = await asyncio.to_thread(
            elo_col.find_one,
            {"_id": repository.player_doc_id(replacement.id, match_queue_type)},
        )
        if elo_doc:
            return int(elo_doc.get("elo", elo_calc.ELO_START))
        return elo_calc.ELO_START

    async def _replace_within_elo_band(
        self,
        interaction: discord.Interaction,
        leaver: discord.Member,
        replacement: discord.Member,
        leaver_elo: int,
        new_elo: int,
    ) -> bool:
        """Reject the replace if |leaver - replacement| > MAX_REPLACE_ELO_DIFF.

        Reasoning: teams were balanced at formation; a big gap breaks the
        balance and the post-match ELO would not reflect real performance.
        """
        elo_diff = abs(leaver_elo - new_elo)
        if elo_diff <= MAX_REPLACE_ELO_DIFF:
            return True
        await interaction.followup.send(
            f"❌ Écart d'ELO trop important : {leaver.mention} "
            f"({leaver_elo}) vs {replacement.mention} ({new_elo}) "
            f"-> écart={elo_diff} > {MAX_REPLACE_ELO_DIFF}. Les équipes "
            "seraient déséquilibrées. Annule le match (`/match-cancel`) "
            "et reforme la file.",
            ephemeral=True,
        )
        return False

    async def _apply_replace_update(
        self,
        match: dict,
        team_key: str,
        leaver_id: int,
        replacement: discord.Member,
        new_elo: int,
    ) -> tuple[bool, bool]:
        """Apply the team swap and (if applicable) lobby-leader transfer.

        The transfer matters because ``_fetch_henrik_multipliers`` queries
        the lobby leader's Riot history; leaving the old leader would make
        Henrik miss the custom and fall back to flat ELO. Updates use a
        CAS on ``status=pending`` to avoid clobbering a concurrent
        vote/cancel.

        Returns ``(leader_replaced, modified)``.
        """
        new_player = {
            "id": replacement.id,
            "name": replacement.display_name,
            "elo": new_elo,
        }
        new_team = [new_player if int(p.get("id", 0)) == leaver_id else p for p in match[team_key]]
        update: dict[str, Any] = {team_key: new_team}
        leader_replaced = int(match.get("lobby_leader_id", 0)) == int(leaver_id)
        if leader_replaced:
            update["lobby_leader_id"] = str(replacement.id)

        matches_col = repository.get_matches_col(self.db)
        result = await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match["_id"], "status": "pending"},
            {"$set": update},
        )
        return leader_replaced, result.modified_count == 1

    @staticmethod
    async def _transfer_match_host_role(
        guild: discord.Guild,
        leaver: discord.Member,
        replacement: discord.Member,
    ) -> None:
        """Move ``MATCH_HOST_ROLE_NAME`` from leaver to replacement. Best-effort."""
        host_role = discord.utils.get(guild.roles, name=MATCH_HOST_ROLE_NAME)
        if host_role is None:
            return
        if host_role in leaver.roles:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await leaver.remove_roles(host_role, reason="Match replace : hôte transféré")
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await replacement.add_roles(host_role, reason="Match replace : hôte transféré")

    @staticmethod
    def _resolve_match_id(match_id: str) -> ObjectId | str:
        """Convert the id entered by the admin into an ObjectId.

        Matches created via `repository.create_match` have an ObjectId
        `_id` (insert_one without `_id`). pymongo does NOT convert a
        hex string into an ObjectId: `{"_id": "<hex>"}` never matches
        a doc with an ObjectId `_id`. We therefore convert explicitly.
        Fallback to the raw value if it is not a valid hex ObjectId, to
        stay compatible with possible legacy docs with a string `_id`.
        """
        try:
            return ObjectId(match_id)
        except (InvalidId, TypeError):
            return match_id

    @app_commands.command(
        name="match-cleanup",
        description="(Admin) Force la suppression de la catégorie d'un match contesté ou bloqué.",
    )
    async def match_cleanup(self, interaction: discord.Interaction, match_id: str) -> None:
        """Admin-only force teardown for disputed/blocked matches."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Cette commande est réservée aux administrateurs.",
                ephemeral=True,
            )
            return

        query_id = self._resolve_match_id(match_id)
        match = self.db["matches"].find_one({"_id": query_id})
        if match is None:
            await interaction.response.send_message(
                f"Match `{match_id}` introuvable.", ephemeral=True
            )
            return

        category_id = match.get("category_id")
        if not category_id:
            await interaction.response.send_message(
                f"Le match `{match_id}` n'a pas de category_id "
                "(probablement un match antérieur à la migration).",
                ephemeral=True,
            )
            return

        # Reuse the doc's real `_id` for the next ops: guarantees we
        # target the right document whatever the id type.
        real_id = match["_id"]
        await asyncio.to_thread(repository.mark_match_cleanup_started, self.db, real_id)
        await delete_match_category(
            guild=interaction.guild,
            category_id=category_id,
            reason=f"Nettoyage admin par {interaction.user} (match {match_id})",
        )
        self.db["matches"].update_one(
            {"_id": real_id},
            {
                "$set": {
                    "status": "cleaned_up",
                    "cleaned_up_at": datetime.now(UTC),
                    "cleaned_up_by": interaction.user.id,
                }
            },
        )
        await interaction.response.send_message(f"Match `{match_id}` nettoyé.", ephemeral=True)

    @app_commands.command(
        name="match-force-result",
        description="Force le vainqueur d'un vote expiré / bloqué dans ce salon (admin)",
    )
    @app_commands.describe(winner="Équipe qui a gagné le match")
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Team A", value="a"),
            app_commands.Choice(name="Team B", value="b"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_force_result(
        self,
        interaction: discord.Interaction,
        winner: app_commands.Choice[str],
    ) -> None:
        """Settle a `contested` (vote timed out) or `pending` match by hand.

        Atomic CAS on status pending/contested + elo_applied != True: a
        concurrent vote reaching the majority, /match-cancel or the Henrik
        ELO claim all make this fail cleanly rather than overwriting an
        already-settled result. On success we fire the exact same
        post-validation hook as a normal vote (category teardown + Henrik
        verification scheduling), so the ELO is applied downstream."""
        await interaction.response.defer(ephemeral=True)
        forced = await asyncio.to_thread(
            repository.force_match_result_atomically,
            self.db,
            channel_id=interaction.channel_id,
            winner=winner.value,
        )
        if not forced:
            await interaction.followup.send(
                "❌ Aucun match forçable dans ce salon "
                "(doit être pending/contested avec ELO non encore appliqué).",
                ephemeral=True,
            )
            return

        team_label = "Team A" if winner.value == "a" else "Team B"
        await interaction.followup.send(
            f"✅ Résultat forcé : **{team_label} a gagné**. "
            f"L'ELO sera appliqué après la vérification HenrikDev.",
            ephemeral=True,
        )

        # Same hook as a vote reaching the majority: deletes the match
        # category and schedules the Henrik verification / ELO pass.
        try:
            await self._on_match_validated(interaction, forced)
        except Exception:
            logger.exception("[match-force-result] _on_match_validated raised")

    @match_cancel.error
    @match_replace.error
    @match_force_result.error
    async def _admin_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            try:
                await inter.response.send_message(
                    "🚫 Réservé aux administrateurs.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await inter.followup.send(
                    "🚫 Réservé aux administrateurs.",
                    ephemeral=True,
                )


async def setup(
    bot: commands.Bot,
    db,
    *,
    rng: random.Random | None = None,
    henrik_client: HenrikDevClient | None = None,
) -> MatchCog:
    cog = MatchCog(bot, db, rng=rng, henrik_client=henrik_client)
    await bot.add_cog(cog)
    bot.add_view(cog.vote_view)
    return cog
