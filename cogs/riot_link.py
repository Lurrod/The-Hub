"""
V2 cog: Discord account <-> Riot account linking.

Commands:
  /link-riot riot_id:Username#TAG     (region forced to EU)
  /unlink-riot
  /admin-unlink-riot membre:@x | riot_id:Name#TAG   (admin)

No gate-keeping: rank verification for new members is done manually
when they enter the Discord server.

The Riot link only persists the Riot account metadata (PUUID,
username, tag) to enable post-match verification via the HenrikDev
API. No ELO is seeded: players start at `ELO_START` (=2000) when
they first appear in a given queue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from pymongo.errors import DuplicateKeyError

from services import repository
from services.riot_api import (
    HenrikDevClient,
    PlayerNotFoundError,
    RateLimitedError,
    RiotApiError,
)
from services.riot_id import parse_riot_id

logger = logging.getLogger(__name__)


# Server reserved to EU
DEFAULT_REGION = "eu"


class RiotLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
        self.bot = bot
        self.db = db
        self.riot_client = riot_client

    # ── /link-riot ────────────────────────────────────────────────
    @app_commands.command(
        name="link-riot", description="Lie ton compte Discord à ton compte Riot (EU)"
    )
    @app_commands.describe(
        riot_id="Ton Riot ID au format Username#TAG (ex. Player#EUW)",
    )
    async def link_riot(
        self,
        interaction: discord.Interaction,
        riot_id: str,
    ) -> None:
        region = DEFAULT_REGION
        # 1) Parse riot_id
        try:
            name, tag = parse_riot_id(riot_id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 2) Verify the Riot account's existence + fetch current rank (display).
        # HenrikDev calls are synchronous (`requests`) and would block the
        # Discord event loop for ~10s if the API is slow. We run them in a
        # thread to preserve the bot's responsiveness.
        try:
            account = await asyncio.to_thread(self.riot_client.get_account, name, tag)
            mmr = await asyncio.to_thread(self.riot_client.get_current_mmr, region, name, tag)
        except PlayerNotFoundError:
            await interaction.followup.send(
                f"❌ Joueur **{name}#{tag}** introuvable.", ephemeral=True
            )
            return
        except RateLimitedError:
            await interaction.followup.send(
                "⏳ API HenrikDev saturée (rate limit), réessaie dans 1 minute.", ephemeral=True
            )
            return
        except RiotApiError as e:
            # Do not leak the raw API response (potentially contains
            # internal details or HTML error excerpts). We log on the
            # server side and return a generic message to the user.
            logger.error(
                f"[link-riot] RiotApiError for user={interaction.user.id}: {e!r}", exc_info=True
            )
            await interaction.followup.send(
                "❌ Erreur temporaire de l'API Riot. Réessaie dans quelques instants.",
                ephemeral=True,
            )
            return

        # 2.5) PUUID dedup: a Riot account can only be linked to a single
        # Discord account per server. Without this check, a player could
        # hold 2 spots in queue with a single in-game account via two
        # Discord accounts linked to the same PUUID.
        existing = await asyncio.to_thread(
            repository.find_riot_account_by_puuid,
            self.db,
            account.puuid,
        )
        if existing is not None and str(existing.get("_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ Le compte Riot **{name}#{tag}** est déjà lié à un autre "
                "membre du serveur. Un compte Riot ne peut être lié qu'à "
                "un seul compte Discord par serveur.",
                ephemeral=True,
            )
            return

        # 2.7) Peak ELO over the last 6 months. Shown to captains in the
        # Open/Draft draft pool (cf. cogs/match/_formation.py). Best-effort: a history
        # fetch failure must NOT block linking — we fall back to the
        # current MMR elo.
        cutoff = datetime.now(UTC) - timedelta(days=180)
        peak_elo_6mo = mmr.elo
        try:
            history = await asyncio.to_thread(self.riot_client.get_mmr_history, region, name, tag)
            for h in history:
                if h.date >= cutoff and h.elo > peak_elo_6mo:
                    peak_elo_6mo = h.elo
        except RiotApiError:
            logger.warning(
                "[link-riot] mmr-history fetch failed for %s#%s; "
                "falling back to current MMR for the 6-month peak",
                name,
                tag,
            )

        # 3) Persist the Riot metadata + the 6-month peak ELO (used to
        # gate-keep and balance the Open queue, and for post-match
        # verification via HenrikDev). Match ELO still starts at ELO_START
        # on the first match in each queue.
        # DuplicateKeyError: race condition with another Discord linking
        # the same PUUID in parallel. The unique index on puuid protects
        # the data - we return the same friendly message as the dedup
        # check above.
        try:
            repository.link_riot_account(
                self.db,
                user_id=interaction.user.id,
                riot_name=name,
                riot_tag=tag,
                riot_region=region,
                puuid=account.puuid,
                peak_elo=peak_elo_6mo,
                source="peak_6mo",
            )
        except DuplicateKeyError:
            await interaction.followup.send(
                f"❌ Le compte Riot **{name}#{tag}** est déjà lié à un autre "
                "membre du serveur. Un compte Riot ne peut être lié qu'à "
                "un seul compte Discord par serveur.",
                ephemeral=True,
            )
            return

        # 5) Confirmation embed
        embed = discord.Embed(
            title="🎯 Compte Riot lié !",
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Riot ID", value=f"**{name}#{tag}**", inline=True)
        embed.add_field(name="Région", value=region.upper(), inline=True)
        embed.add_field(name="Rang actuel", value=mmr.tier_name, inline=True)
        embed.add_field(name="Peak ELO (6 mois)", value=str(peak_elo_6mo), inline=True)
        embed.set_footer(text=f"Discord: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unlink-riot ──────────────────────────────────────────────
    @app_commands.command(name="unlink-riot", description="Supprime le lien vers ton compte Riot")
    async def unlink_riot(self, interaction: discord.Interaction) -> None:
        ok = repository.unlink_riot_account(
            self.db,
            interaction.user.id,
        )
        if ok:
            await interaction.response.send_message("✅ Compte Riot délié.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Aucun compte Riot lié.", ephemeral=True)

    # ── /admin-unlink-riot ────────────────────────────────────────
    @app_commands.command(
        name="admin-unlink-riot",
        description="Délier le compte Riot d'un membre ou un Riot ID (admin)",
    )
    @app_commands.describe(
        membre="Membre dont on supprime le lien Riot",
        riot_id="Riot ID à délier (Name#TAG), utile si le membre a quitté le serveur",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def admin_unlink_riot(
        self,
        interaction: discord.Interaction,
        membre: discord.User | None = None,
        riot_id: str | None = None,
    ) -> None:
        if (membre is None) == (riot_id is None):
            await interaction.response.send_message(
                "❌ Indique soit `membre`, soit `riot_id` (un seul des deux).",
                ephemeral=True,
            )
            return

        if membre is not None:
            target_id = str(membre.id)
            doc = repository.get_riot_account(self.db, target_id)
            if doc is None:
                await interaction.response.send_message(
                    f"ℹ️ Aucun compte Riot lié à <@{target_id}>.", ephemeral=True
                )
                return
        else:
            try:
                name, tag = parse_riot_id(riot_id or "")
            except ValueError:
                await interaction.response.send_message(
                    "❌ Format invalide. Attendu : `Name#TAG`.", ephemeral=True
                )
                return
            doc = repository.find_riot_account_by_riot_id(self.db, name, tag)
            if doc is None:
                await interaction.response.send_message(
                    f"ℹ️ Aucun compte lié au Riot ID **{name}#{tag}**.", ephemeral=True
                )
                return
            target_id = str(doc["_id"])

        repository.unlink_riot_account(self.db, target_id)
        linked = f"{doc.get('riot_name', '?')}#{doc.get('riot_tag', '?')}"
        logger.info(
            "[admin-unlink-riot] admin=%s unlinked %s from user=%s",
            interaction.user.id,
            linked,
            target_id,
        )
        await interaction.response.send_message(
            f"✅ Compte Riot **{linked}** délié de <@{target_id}>.", ephemeral=True
        )

    @admin_unlink_riot.error
    async def _admin_unlink_riot_error(self, interaction: discord.Interaction, error) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "🚫 Réservé aux administrateurs.", ephemeral=True
            )


async def setup(bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
    await bot.add_cog(RiotLinkCog(bot, db, riot_client))
