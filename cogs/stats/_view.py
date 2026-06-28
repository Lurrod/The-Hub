"""Two-page paginator for /stats. The buttons have stable custom_ids
so the view could be re-registered with `bot.add_view` at startup if
session-persistence is desired (current default: ephemeral, view dies
on timeout).
"""

from __future__ import annotations

import discord


class StatsPaginatorView(discord.ui.View):
    """Two-page paginator. Stores the two pre-built embeds + the
    invoker_id; flips `page` on button press and re-edits the
    interaction message. timeout=300s.
    """

    def __init__(
        self,
        *,
        overview: discord.Embed,
        details: discord.Embed,
        invoker_id: int,
        timeout: float | None = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.overview = overview
        self.details = details
        self.invoker_id = invoker_id
        self.page = 0  # 0 = overview, 1 = details

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "❌ Ce panneau de stats n'est pas le vôtre.", ephemeral=True
            )
            return False
        return True

    def _current_embed(self) -> discord.Embed:
        return self.overview if self.page == 0 else self.details

    @discord.ui.button(custom_id="stats_prev", emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button) -> None:
        self.page = 0
        await interaction.response.edit_message(embed=self._current_embed(), view=self)

    @discord.ui.button(custom_id="stats_next", emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button) -> None:
        self.page = 1
        await interaction.response.edit_message(embed=self._current_embed(), view=self)
