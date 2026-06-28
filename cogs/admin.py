"""
Admin cog: utility commands (/setup, /bypass, /map, /coinflip,
/clear, /help). Extracted from bot.py (monolith refactor).

`/setup` creates the category + the channels and posts the 2 queue
messages by delegating to QueueCog.post_queue_message and
refresh_leaderboard_channel.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from services import elo_calc, repository
from services.leaderboard_refresh import refresh_leaderboard_channel

logger = logging.getLogger(__name__)


SETUP_CATEGORY_NAME = "🎮 Valorant 10mans"
# 2 files de résultats + 2 files d'attente + 1 leaderboard partagé + 1 matchs.
SETUP_CHANNELS = [
    "leaderboard",
    "open-queue",
    "advanced-queue",
    "draft-queue",
    "open-results",
    "advanced-results",
    "draft-results",
    "matchs",
]
# Mapping queue_type -> channel name to post the persistent message in.
QUEUE_CHANNEL_FOR_TYPE = {
    "open": "open-queue",
    "advanced": "advanced-queue",
    "draft": "draft-queue",
}


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OR bypass role configured via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /setup ─────────────────────────────────────────────────
    @app_commands.command(
        name="setup", description="Crée la catégorie et les salons requis par le bot"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_bot(self, interaction: discord.Interaction):
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        # 1) Category
        category = discord.utils.get(guild.categories, name=SETUP_CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(SETUP_CATEGORY_NAME)
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ Le bot ne dispose pas de la permission **Gérer les salons**.",
                    ephemeral=True,
                )
                return

        # 2) Channels
        created: list[str] = []
        existed: list[str] = []
        for name in SETUP_CHANNELS:
            chan = discord.utils.get(guild.text_channels, name=name)
            if chan is None:
                try:
                    await guild.create_text_channel(name, category=category)
                    created.append(name)
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"❌ Impossible de créer `#{name}` (permissions manquantes).",
                        ephemeral=True,
                    )
                    return
            else:
                existed.append(name)

        # 3) Post the persistent message of each queue in its dedicated channel
        queue_cog = self.bot.get_cog("QueueCog")
        queue_status: list[str] = []
        if queue_cog is not None:
            for qt in repository.QUEUE_TYPES:
                channel_name = QUEUE_CHANNEL_FOR_TYPE[qt]
                chan = discord.utils.get(guild.text_channels, name=channel_name)
                if chan is None:
                    queue_status.append(f"⚠️ Salon `#{channel_name}` introuvable.")
                    continue
                repository.delete_active_queue(self.db, guild.id, qt)
                try:
                    await queue_cog.post_queue_message(chan, qt)  # type: ignore[attr-defined]
                    queue_status.append(f"🎯 File {qt.upper()} publiée dans {chan.mention}")
                except discord.Forbidden:
                    queue_status.append(f"⚠️ Impossible d'envoyer dans {chan.mention} (permissions)")

        # 4) Pre-post the leaderboards (silently skip if 0 players).
        # Draft has no leaderboard, so only the ranked queues are posted.
        for qt in repository.RANKED_QUEUE_TYPES:
            try:
                await refresh_leaderboard_channel(guild, self.db, qt)
            except Exception:
                logger.exception("[setup] pre-post leaderboard %s raised", qt)

        # 5) Recap
        lines: list[str] = []
        if created:
            lines.append(f"✅ Créés : {', '.join(f'`#{c}`' for c in created)}")
        if existed:
            lines.append(f"ℹ️ Déjà présents : {', '.join(f'`#{c}`' for c in existed)}")
        lines.extend(queue_status)
        if not lines:
            lines.append("✅ Configuration terminée.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @setup_bot.error
    async def _setup_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Réservé aux administrateurs.",
                ephemeral=True,
            )

    # ── /bypass ────────────────────────────────────────────────
    @app_commands.command(
        name="bypass", description="Donne à un rôle l'accès à toutes les commandes du bot"
    )
    @app_commands.describe(role="Le rôle qui obtiendra l'accès à toutes les commandes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bypass(self, interaction: discord.Interaction, role: discord.Role):
        if role.id == interaction.guild_id or role.is_default():
            await interaction.response.send_message(
                "❌ Impossible d'accorder le bypass à @everyone - cela donnerait l'accès admin à tout le serveur.",
                ephemeral=True,
            )
            return
        if role.managed:
            await interaction.response.send_message(
                "❌ Impossible d'accorder le bypass à un rôle géré par une intégration (bot, booster, etc.).",
                ephemeral=True,
            )
            return
        repository.set_bypass_role(self.db, interaction.guild_id, role.id)
        embed = discord.Embed(
            title="🔓 Bypass activé !",
            description=f"Le rôle {role.mention} a désormais accès à toutes les commandes du bot.",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Configuré par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bypass.error
    async def _bypass_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Seuls les administrateurs peuvent configurer le bypass.", ephemeral=True
            )

    # ── /map ───────────────────────────────────────────────────
    @app_commands.command(name="map", description="Choisit une map aléatoire pour la partie")
    async def map_pick(self, interaction: discord.Interaction):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "🚫 Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True
            )
            return
        chosen = random.choice(elo_calc.MAPS)
        embed = discord.Embed(
            title="🗺️ Map sélectionnée !",
            description=f"## {chosen}",
            color=0x9B59B6,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Tirée par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /coinflip ──────────────────────────────────────────────
    @app_commands.command(name="coinflip", description="Lance une pièce")
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Pile", "Face"])
        embed = discord.Embed(
            title="🪙 Pile ou Face !",
            description=f"## {result}",
            color=0xF1C40F,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Lancée par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /clear ─────────────────────────────────────────────────
    @app_commands.command(
        name="clear", description="Supprime un certain nombre de messages dans le salon"
    )
    @app_commands.describe(amount="Nombre de messages à supprimer (max 100)")
    async def clear(self, interaction: discord.Interaction, amount: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("Pas de permission.", ephemeral=True)
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "Le nombre doit être compris entre 1 et 100.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        embed = discord.Embed(
            title="🗑️ Messages supprimés",
            description=f"**{len(deleted)}** message(s) supprimé(s).",
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /help ──────────────────────────────────────────────────
    @app_commands.command(name="help", description="Affiche la liste des commandes disponibles")
    @app_commands.describe(kind="Choisissez le type d'aide")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Commandes membres", value="members"),
            app_commands.Choice(name="Commandes admin", value="admin"),
        ]
    )
    @app_commands.rename(kind="type")
    async def help_cmd(self, interaction: discord.Interaction, kind: str = "members"):
        if kind == "admin":
            if not _has_access(interaction, self.db):
                await interaction.response.send_message("Pas de permission.", ephemeral=True)
                return
            embed = discord.Embed(
                title="⚙️ Commandes admin", color=0xE74C3C, timestamp=datetime.now(UTC)
            )
            embed.add_field(
                name="/setup",
                value="Crée la catégorie + les salons (`leaderboard`, `open-queue`, `advanced-queue`, `open-results`, `advanced-results`, `matchs`) et publie les 2 messages de file",
                inline=False,
            )
            embed.add_field(
                name="/setup-queue queue",
                value="Republie le message persistant d'une file (open/advanced)",
                inline=False,
            )
            embed.add_field(
                name="/close-queue queue", value="Ferme la file active d'un type", inline=False
            )
            embed.add_field(
                name="/win queue @p1..@p5",
                value="Victoire - ELO pondéré selon la position",
                inline=False,
            )
            embed.add_field(
                name="/lose queue @p1..@p5",
                value="Défaite - ELO pondéré selon la position",
                inline=False,
            )
            embed.add_field(name="/map", value="Map aléatoire", inline=False)
            embed.add_field(
                name="/elomodify queue @p action amount",
                value="Ajoute ou retire de l'ELO à un joueur dans une file",
                inline=False,
            )
            embed.add_field(
                name="/winmodify queue @p action amount",
                value="Ajoute ou retire des victoires",
                inline=False,
            )
            embed.add_field(
                name="/losemodify queue @p action amount",
                value="Ajoute ou retire des défaites",
                inline=False,
            )
            embed.add_field(
                name="/resetelo queue [@player|all]",
                value=f"Réinitialise l'ELO d'un joueur (ou de tous) à {elo_calc.ELO_START} dans une file",
                inline=False,
            )
            embed.add_field(
                name="/reset-queue queue",
                value="Réinitialisation complète d'une file (ELO + matchs + leaderboard) - confirmation requise",
                inline=False,
            )
            embed.add_field(
                name="/bypass @role",
                value="Donne à un rôle l'accès aux commandes admin",
                inline=False,
            )
            embed.add_field(name="/clear amount", value="Supprime des messages", inline=False)
            embed.set_footer(text=f"Demandé par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="📖 Commandes disponibles", color=0x3498DB, timestamp=datetime.now(UTC)
            )
            embed.add_field(
                name="/leaderboard queue",
                value="Classement ELO d'une file (open/advanced)",
                inline=False,
            )
            embed.add_field(
                name="/stats queue [@player]",
                value="Stats d'un joueur dans une file. Sans mention = vos propres stats",
                inline=False,
            )
            embed.add_field(name="/help", value="Affiche cette aide", inline=False)
            embed.set_footer(text=f"Demandé par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(AdminCog(bot, db))
