"""/stats slash command.

Reads ELO + rating aggregate, renders a 2-page paginated embed via
StatsPaginatorView. Forward-only data model: pre-deployment matches
show ELO only with a hint footer."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.stats._embeds import (
    QUEUE_LABELS,
    build_details_embed,
    build_overview_embed,
)
from cogs.stats._view import StatsPaginatorView
from services import repository

_QUEUE_CHOICES = [
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="Advanced", value="advanced"),
]


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    @app_commands.command(
        name="stats",
        description="Affiche les stats d'un joueur dans une file (ELO + Rating 2.0)",
    )
    @app_commands.describe(
        queue="Type de file",
        player="Le joueur dont vous voulez voir les stats",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def stats(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member | None = None,
    ) -> None:
        member = player or interaction.user
        elo_col = repository.get_elo_col(self.db)
        doc_id = repository.player_doc_id(member.id, queue)
        elo_doc = elo_col.find_one({"_id": doc_id})

        if elo_doc is None:
            await interaction.response.send_message(
                f"{member.display_name} n'a pas encore joué dans {QUEUE_LABELS[queue]}.",
                ephemeral=True,
            )
            return

        rank = (
            elo_col.count_documents(
                {
                    "queue_type": queue,
                    "$or": [
                        {"elo": {"$gt": elo_doc["elo"]}},
                        {"elo": elo_doc["elo"], "wins": {"$gt": elo_doc.get("wins", 0)}},
                        {
                            "elo": elo_doc["elo"],
                            "wins": elo_doc.get("wins", 0),
                            "_id": {"$lt": doc_id},
                        },
                    ],
                }
            )
            + 1
        )

        agg = repository.get_rating_aggregate(self.db, user_id=member.id, queue_type=queue)

        overview = build_overview_embed(
            elo_doc=elo_doc,
            rank=rank,
            agg=agg,
            member=member,
            queue_type=queue,
        )

        if agg is None:
            # Pre-deployment data — single-page ELO embed.
            await interaction.response.send_message(embed=overview, ephemeral=True)
            return

        details = build_details_embed(
            agg=agg,
            member=member,
            queue_type=queue,
        )
        view = StatsPaginatorView(
            overview=overview,
            details=details,
            invoker_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=overview,
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot, db) -> StatsCog:
    cog = StatsCog(bot, db)
    await bot.add_cog(cog)
    return cog
