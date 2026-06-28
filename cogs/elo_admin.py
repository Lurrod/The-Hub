"""
ELO admin cog: /win, /lose, /elomodify, /winmodify, /losemodify, /resetelo,
/reset-queue, /leaderboard, /inactivity. Extracted from bot.py (monolith refactor).

Admin commands reserved to manage_guild OR bypass role.
`/leaderboard` is public in #leaderboard, ephemeral elsewhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument

from services import elo_calc, repository
from services.inactivity import (
    DEFAULT_INACTIVITY_LIMIT,
    format_inactivity,
    rank_by_inactivity,
)
from services.leaderboard_refresh import (
    build_leaderboard_payload,
    refresh_leaderboard_channel,
)

logger = logging.getLogger(__name__)


ELO_START = elo_calc.ELO_START

# ELO weighting by player position (slot 1..5) for /win and /lose.
# The first slot takes the biggest gain / the smallest loss.
WIN_DELTAS_BY_SLOT: tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)

# Mapping queue_type -> channel name where to post the persistent message.
# (Intentionally duplicated from cogs/admin.py to avoid an inter-cog
# dependency: this mapping is very stable, and the duplication avoids a
# `from cogs.admin import ...` that would create an import cycle.)
QUEUE_CHANNEL_FOR_TYPE = {
    "open": "open-queue",
    "advanced": "advanced-queue",
}

_QUEUE_CHOICES = [
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="Advanced", value="advanced"),
]

# Human-friendly queue labels for embeds and leaderboard titles.
QUEUE_LABELS = {
    "open": "Open Queue",
    "advanced": "Advanced Queue",
}


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OR bypass role configured via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _get_player(col, member: discord.Member, queue_type: str):
    return repository.get_or_create_player(
        col,
        member.id,
        queue_type,
        member.display_name,
        initial_elo=ELO_START,
    )


def _match_elo_for_member(db, guild_id: int, user_id: int, queue_type: str) -> int:
    """Server ELO of the player in the given queue, falling back to ELO_REFERENCE."""
    doc = repository.get_elo_col(db).find_one(
        {"_id": repository.player_doc_id(user_id, queue_type)}
    )
    if doc and doc.get("elo") is not None:
        return int(doc["elo"])
    return elo_calc.ELO_REFERENCE


def _compute_match_change_for_members(
    db,
    guild_id: int,
    members: list,
    queue_type: str,
) -> tuple[int, int, int]:
    """(avg_elo, gain, loss) for the list of players in the queue."""
    elos = [_match_elo_for_member(db, guild_id, m.id, queue_type) for m in members]
    avg = round(sum(elos) / len(elos)) if elos else elo_calc.ELO_REFERENCE
    gain, loss = elo_calc.compute_match_elo_change(avg)
    return avg, gain, loss


async def _refresh_leaderboard_safe(guild: discord.Guild | None, db, queue_type: str) -> None:
    """Refresh the leaderboard of the given queue in `#leaderboard`."""
    if guild is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, queue_type)
    except Exception:
        logger.exception("[leaderboard] refresh raised")


def _is_leaderboard_channel(interaction: discord.Interaction) -> bool:
    chan = interaction.channel
    name = getattr(chan, "name", "") or ""
    return "leaderboard" in name.lower()


class _ResetQueueConfirmView(discord.ui.View):
    """Interactive confirmation button for /reset-queue."""

    def __init__(self, queue_type: str, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.queue_type = queue_type
        self.confirmed = False

    @discord.ui.button(label="Confirmer la réinitialisation", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class ELOAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /win ───────────────────────────────────────────────────
    @app_commands.command(
        name="win",
        description="Enregistrer une victoire dans une file (gains pondérés par position)",
    )
    @app_commands.describe(
        queue="Type de file",
        player1="Joueur gagnant 1",
        player2="Joueur gagnant 2",
        player3="Joueur gagnant 3",
        player4="Joueur gagnant 4",
        player5="Joueur gagnant 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def win(
        self,
        interaction: discord.Interaction,
        queue: str,
        player1: discord.Member,
        player2: discord.Member = None,
        player3: discord.Member = None,
        player4: discord.Member = None,
        player5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        players = [p for p in [player1, player2, player3, player4, player5] if p is not None]
        col = repository.get_elo_col(self.db)

        deltas = list(WIN_DELTAS_BY_SLOT)[: len(players)]
        avg_elo, _, _ = _compute_match_change_for_members(
            self.db,
            interaction.guild_id,
            players,
            queue,
        )
        desc = f"ELO moyen du groupe : **{avg_elo}** -> gains pondérés par position."

        embed = discord.Embed(
            title=f"Résultats {QUEUE_LABELS[queue]} - Victoire enregistrée !",
            description=desc,
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            gain = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                {"$inc": {"elo": gain, "wins": 1}},
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = old + gain
            embed.add_field(
                name=member.display_name,
                value=f"+{gain} ELO -> **{new}** *(avant {old})*",
                inline=False,
            )
        embed.set_footer(text=f"Enregistré par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /lose ──────────────────────────────────────────────────
    @app_commands.command(
        name="lose",
        description="Enregistrer une défaite dans une file (pertes pondérées par position)",
    )
    @app_commands.describe(
        queue="Type de file",
        player1="Joueur perdant 1",
        player2="Joueur perdant 2",
        player3="Joueur perdant 3",
        player4="Joueur perdant 4",
        player5="Joueur perdant 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def lose(
        self,
        interaction: discord.Interaction,
        queue: str,
        player1: discord.Member,
        player2: discord.Member = None,
        player3: discord.Member = None,
        player4: discord.Member = None,
        player5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        players = [p for p in [player1, player2, player3, player4, player5] if p is not None]
        col = repository.get_elo_col(self.db)

        deltas = list(LOSE_DELTAS_BY_SLOT)[: len(players)]
        avg_elo, _, _ = _compute_match_change_for_members(
            self.db,
            interaction.guild_id,
            players,
            queue,
        )
        desc = f"ELO moyen du groupe : **{avg_elo}** -> pertes pondérées par position."

        embed = discord.Embed(
            title=f"Résultats {QUEUE_LABELS[queue]} - Défaite enregistrée !",
            description=desc,
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            loss = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                [
                    {
                        "$set": {
                            "elo": {"$max": [0, {"$subtract": [{"$ifNull": ["$elo", 0]}, loss]}]},
                            "losses": {"$add": [{"$ifNull": ["$losses", 0]}, 1]},
                        }
                    }
                ],
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = max(0, old - loss)
            embed.add_field(
                name=member.display_name,
                value=f"-{loss} ELO -> **{new}** (avant {old})",
                inline=False,
            )
        embed.set_footer(text=f"Enregistré par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /leaderboard ───────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="Afficher le classement ELO d'une file")
    @app_commands.describe(queue="Type de file")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def leaderboard(self, interaction: discord.Interaction, queue: str):
        public = _is_leaderboard_channel(interaction)
        ephemeral = not public
        await interaction.response.defer(ephemeral=ephemeral)
        file, view = await build_leaderboard_payload(interaction.guild, self.db, queue)
        if file is None:
            await interaction.followup.send(
                f"Aucun joueur enregistré dans la {QUEUE_LABELS[queue]}.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file, view=view, ephemeral=ephemeral)

    # ── /resetelo ──────────────────────────────────────────────
    @app_commands.command(
        name="resetelo",
        description=f"Réinitialiser l'ELO d'un joueur (ou de tous) à {ELO_START} dans une file",
    )
    @app_commands.describe(
        queue="Type de file",
        player="Le joueur à réinitialiser à la valeur initiale",
        all_players=f"Réinitialiser l'ELO de tous les joueurs de cette file à {ELO_START}",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.rename(all_players="all")
    async def resetelo(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member = None,
        all_players: bool = False,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        col = repository.get_elo_col(self.db)
        if all_players:
            count = col.count_documents({"queue_type": queue})
            col.update_many(
                {"queue_type": queue},
                {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
            )
            embed = discord.Embed(
                title=f"🔄 Réinitialisation globale - {QUEUE_LABELS[queue]} !",
                description=f"ELO de **{count} joueur(s)** réinitialisé à {ELO_START} dans la {QUEUE_LABELS[queue]}.",
                color=0xE74C3C,
                timestamp=datetime.now(UTC),
            )
            embed.set_footer(text=f"Réinitialisé par {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed)
            await _refresh_leaderboard_safe(interaction.guild, self.db, queue)
            return
        if player is None:
            await interaction.response.send_message(
                "Mentionnez un joueur ou utilisez `all:True`.", ephemeral=True
            )
            return
        doc = _get_player(col, player, queue)
        old = doc["elo"]
        col.update_one(
            {"_id": repository.player_doc_id(player.id, queue)},
            {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
        )
        embed = discord.Embed(
            title=f"🔄 ELO {QUEUE_LABELS[queue]} réinitialisé !",
            color=0x95A5A6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Joueur", value=player.mention, inline=True)
        embed.add_field(name="Ancien ELO", value=str(old), inline=True)
        embed.add_field(name="Nouvel ELO", value=str(ELO_START), inline=True)
        embed.set_thumbnail(url=player.display_avatar.url)
        embed.set_footer(text=f"Réinitialisé par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /reset-queue ───────────────────────────────────────────
    @app_commands.command(
        name="reset-queue", description="Supprimer toutes les données d'une file (admin)"
    )
    @app_commands.describe(queue="Type de file à réinitialiser")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_queue(self, interaction: discord.Interaction, queue: str):
        view = _ResetQueueConfirmView(queue_type=queue)
        embed = discord.Embed(
            title=f"⚠️ Réinitialiser {QUEUE_LABELS[queue]}",
            description=(
                f"Cette action va **supprimer définitivement** :\n"
                f"- Tout l'ELO de la {QUEUE_LABELS[queue]}\n"
                f"- L'historique des matchs de la {QUEUE_LABELS[queue]}\n"
                f"- L'état du classement de la {QUEUE_LABELS[queue]}\n\n"
                f"Les autres files ne sont pas affectées. **Confirmer ?**"
            ),
            color=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            await interaction.followup.send(
                "Réinitialisation annulée (délai dépassé ou non confirmée).",
                ephemeral=True,
            )
            return

        elo_col = repository.get_elo_col(self.db)
        elo_col.delete_many({"queue_type": queue})
        repository.delete_active_queue(self.db, interaction.guild_id, queue)
        matches_col = repository.get_matches_col(self.db)
        matches_col.delete_many({"queue_type": queue})
        repository.clear_leaderboard_message_id(self.db, interaction.guild_id, queue)

        # Re-post the queue message in the correct channel
        queue_cog = self.bot.get_cog("QueueCog")
        target_name = QUEUE_CHANNEL_FOR_TYPE[queue]
        target_chan = discord.utils.get(interaction.guild.text_channels, name=target_name)
        if queue_cog and target_chan:
            try:
                await queue_cog.post_queue_message(target_chan, queue)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("[reset-queue] re-post queue raised")

        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

        audit = discord.Embed(
            title=f"🔄 {QUEUE_LABELS[queue]} réinitialisée",
            description=f"Réinitialisation effectuée par {interaction.user.mention}",
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        try:
            await interaction.channel.send(embed=audit)
        except Exception:
            logger.exception("[reset-queue] audit log raised")
        await interaction.followup.send(
            f"✅ {QUEUE_LABELS[queue]} réinitialisée.",
            ephemeral=True,
        )

    @reset_queue.error
    async def _reset_queue_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Réservé aux administrateurs.",
                ephemeral=True,
            )

    # ── /elomodify ─────────────────────────────────────────────
    @app_commands.command(
        name="elomodify", description="Ajouter ou retirer de l'ELO à un joueur dans une file"
    )
    @app_commands.describe(
        queue="Type de file",
        player="Le joueur",
        action="Ajouter ou retirer",
        amount="Quantité d'ELO",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Retirer", value="remove"),
        ],
    )
    async def elomodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ La quantité doit être strictement positive. Utilisez l'action `- Retirer` pour enlever de l'ELO.",
                ephemeral=True,
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"elo": {"$max": [0, {"$add": [{"$ifNull": ["$elo", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("elo", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ECC71
            label = f"+{amount}"
            title = f"➕ ELO {QUEUE_LABELS[queue]} ajouté"
        else:
            color = 0xE74C3C
            label = f"-{amount}"
            title = f"➖ ELO {QUEUE_LABELS[queue]} retiré"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur", value=player.mention, inline=True)
        embed.add_field(name="Changement", value=label, inline=True)
        embed.add_field(name="Nouvel ELO", value=f"**{new}** (avant {old})", inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /winmodify ─────────────────────────────────────────────
    @app_commands.command(
        name="winmodify", description="Ajouter ou retirer des victoires à un joueur dans une file"
    )
    @app_commands.describe(
        queue="Type de file",
        player="Le joueur",
        action="Ajouter ou retirer",
        amount="Nombre de victoires",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Retirer", value="remove"),
        ],
    )
    async def winmodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ La quantité doit être strictement positive.", ephemeral=True
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"wins": {"$max": [0, {"$add": [{"$ifNull": ["$wins", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("wins", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ECC71
            label = f"+{amount}"
            title = f"➕ Victoires {QUEUE_LABELS[queue]} ajoutées"
        else:
            color = 0xE74C3C
            label = f"-{amount}"
            title = f"➖ Victoires {QUEUE_LABELS[queue]} retirées"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur", value=player.mention, inline=True)
        embed.add_field(name="Changement", value=label, inline=True)
        embed.add_field(name="Nouveau total", value=f"**{new}** (avant {old})", inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /losemodify ────────────────────────────────────────────
    @app_commands.command(
        name="losemodify", description="Ajouter ou retirer des défaites à un joueur dans une file"
    )
    @app_commands.describe(
        queue="Type de file",
        player="Le joueur",
        action="Ajouter ou retirer",
        amount="Nombre de défaites",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Ajouter", value="add"),
            app_commands.Choice(name="- Retirer", value="remove"),
        ],
    )
    async def losemodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ La quantité doit être strictement positive.", ephemeral=True
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"losses": {"$max": [0, {"$add": [{"$ifNull": ["$losses", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("losses", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0xE74C3C
            label = f"+{amount}"
            title = f"➕ Défaites {QUEUE_LABELS[queue]} ajoutées"
        else:
            color = 0x2ECC71
            label = f"-{amount}"
            title = f"➖ Défaites {QUEUE_LABELS[queue]} retirées"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Joueur", value=player.mention, inline=True)
        embed.add_field(name="Changement", value=label, inline=True)
        embed.add_field(name="Nouveau total", value=f"**{new}** (avant {old})", inline=True)
        embed.set_footer(text=f"Par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /inactivity ────────────────────────────────────────────
    @app_commands.command(
        name="inactivity",
        description="Afficher les joueurs les plus inactifs d'une file",
    )
    @app_commands.describe(queue="Type de file")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def inactivity(self, interaction: discord.Interaction, queue: str):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "Vous n'avez pas la permission.", ephemeral=True
            )
            return

        col = repository.get_elo_col(self.db)
        docs = list(col.find({"queue_type": queue}))
        ranked = rank_by_inactivity(docs, limit=DEFAULT_INACTIVITY_LIMIT)

        if not ranked:
            await interaction.response.send_message(
                f"Aucun joueur dans la {QUEUE_LABELS[queue]}.", ephemeral=True
            )
            return

        now = datetime.now(UTC)
        lines = []
        for rank, doc in enumerate(ranked, start=1):
            user_id = doc.get("user_id") or str(doc["_id"]).rsplit(":", 1)[0]
            duration = format_inactivity(doc.get("last_played"), now)
            lines.append(f"`{rank:>2}.` <@{user_id}> - {duration}")

        embed = discord.Embed(
            title=f"Inactivité - {QUEUE_LABELS[queue]}",
            description="\n".join(lines),
            color=discord.Color.orange(),
            timestamp=now,
        )
        embed.set_footer(text=f"Top {len(ranked)} des joueurs les plus inactifs")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(ELOAdminCog(bot, db))
