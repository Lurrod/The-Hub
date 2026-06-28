"""Persistent VoteView: 2 "Team A/B won" buttons.

Logic:
- look up the match by message_id
- atomic CAS on status=pending to avoid late votes and double-validation
- transition_match_status (CAS) fires `on_validated` ONLY for the vote
  that tips the majority (not for concurrent votes arriving afterwards).
"""

from __future__ import annotations

import asyncio
import logging

import discord

from cogs.match._constants import MAJORITY_THRESHOLD, VOTE_A_BTN_ID, VOTE_B_BTN_ID
from cogs.match._embeds import build_match_embed_from_doc
from services import repository

logger = logging.getLogger(__name__)


class VoteView(discord.ui.View):
    """Persistent view: report the winner of the match (Team A / Team B)."""

    def __init__(self, db, on_validated=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.on_validated = on_validated  # callable(inter, match_doc) -> awaitable

    async def _vote(self, inter: discord.Interaction, choice: str) -> None:
        # 1) Look up the match via the message_id
        match = await asyncio.to_thread(
            repository.get_match_by_message,
            self.db,
            inter.message.id,
        )
        if not match:
            await inter.response.send_message("❌ Match introuvable.", ephemeral=True)
            return

        # 2) Match already validated -> reject
        if match.get("status") in ("validated_a", "validated_b"):
            await inter.response.send_message(
                "✅ Ce match est déjà validé.",
                ephemeral=True,
            )
            return

        # 3) Verify participation
        all_player_ids = {str(p["id"]) for p in (match.get("team_a", []) + match.get("team_b", []))}
        if str(inter.user.id) not in all_player_ids:
            await inter.response.send_message(
                "❌ Tu n'as pas joué ce match, tu ne peux pas voter.",
                ephemeral=True,
            )
            return

        # 4) Record the vote (overwrites a previous vote). CAS on
        # status=pending: if the match has been cancelled/contested/validated
        # in the meantime, the vote is rejected cleanly.
        updated = await asyncio.to_thread(
            repository.add_match_vote,
            self.db,
            match["_id"],
            inter.user.id,
            choice,
        )
        if updated is None:
            await inter.response.send_message(
                "❌ Ce match n'est plus en phase de vote (annulé, contesté ou déjà validé).",
                ephemeral=True,
            )
            return

        # 5) Count
        votes = updated.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        # 6) Majority reached? Atomic transition (CAS) to avoid a
        #    concurrent vote validating twice and triggering
        #    `on_validated` multiple times.
        target_status = None
        if count_a >= MAJORITY_THRESHOLD:
            target_status = "validated_a"
        elif count_b >= MAJORITY_THRESHOLD:
            target_status = "validated_b"

        transitioned_doc = None
        if target_status:
            transitioned_doc = await asyncio.to_thread(
                lambda: repository.transition_match_status(
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status=target_status,
                ),
            )
            if transitioned_doc is not None:
                updated = transitioned_doc
            else:
                # Another concurrent vote already validated. We re-fetch
                # to display the real state without firing `on_validated`
                # from our side.
                fetched = await asyncio.to_thread(
                    repository.get_match,
                    self.db,
                    match["_id"],
                )
                updated = fetched or updated

        # 7) Edit the message (updated embed, view removed if validated)
        embed = build_match_embed_from_doc(updated, inter.guild.name)
        if updated.get("status") in ("validated_a", "validated_b"):
            await inter.response.edit_message(embed=embed, view=None)
        else:
            await inter.response.edit_message(embed=embed, view=self)

        # 8) Phase 6 hook: ELO update. Fires ONLY if the CAS transition
        #    succeeded on our side (i.e. this vote is the one that
        #    tipped the match).
        if transitioned_doc is not None and self.on_validated:
            try:
                await self.on_validated(inter, transitioned_doc)
            except Exception:
                logger.exception("[vote] on_validated raised")

    @discord.ui.button(
        label="Team A a gagné",
        style=discord.ButtonStyle.primary,
        custom_id=VOTE_A_BTN_ID,
    )
    async def vote_a(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "a")

    @discord.ui.button(
        label="Team B a gagné",
        style=discord.ButtonStyle.primary,
        custom_id=VOTE_B_BTN_ID,
    )
    async def vote_b(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "b")
