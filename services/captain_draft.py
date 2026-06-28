"""
Pro Queue Captain Draft Service.

Isolated module for the pro queue only. Contains:
  - pick_captains : selection of the 2 captains (random draw)
  - DraftState    : immutable draft state
  - CaptainDraftSession : Discord orchestration (UI + state machine)

Open and GC queues do NOT use this module: they keep
going through plan_match (auto-balance).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from services.team_balancer import Player

logger = logging.getLogger(__name__)


def pick_captains(
    players: Sequence[Player],
    *,
    rng: random.Random,
) -> tuple[Player, Player]:
    """Pick 2 captains by uniform random draw.

    Args:
        players: list of Player (typically 10).
        rng: seeded random.Random (for test reproducibility).

    Returns:
        (cap_a, cap_b) : two distinct players drawn at random
        from the list. ELO plays no part in the selection.
    """
    if len(players) < 2:
        raise ValueError(f"At least 2 players are required, got {len(players)}")

    cap_a, cap_b = rng.sample(list(players), 2)
    return cap_a, cap_b


# Snake order ABBAABBA. Across 8 picks, capA picks at indices
# 0, 3, 4, 7 and capB at indices 1, 2, 5, 6. With the 2 captains already
# on their team, each side ends up with 5 players (1 cap + 4 picks).
PICK_SEQUENCE: tuple[Literal["A", "B"], ...] = (
    "A",
    "B",
    "B",
    "A",
    "A",
    "B",
    "B",
    "A",
)

DraftStatus = Literal["picking", "complete", "cancelled"]


@dataclass(frozen=True)
class DraftState:
    cap_a: Player
    cap_b: Player
    team_a: tuple[Player, ...]
    team_b: tuple[Player, ...]
    pool: tuple[Player, ...]
    turn_index: int
    status: DraftStatus

    @classmethod
    def initial(
        cls,
        *,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
    ) -> DraftState:
        return cls(
            cap_a=cap_a,
            cap_b=cap_b,
            team_a=(cap_a,),
            team_b=(cap_b,),
            pool=tuple(pool),
            turn_index=0,
            status="picking",
        )

    @property
    def is_complete(self) -> bool:
        return self.turn_index >= len(PICK_SEQUENCE)

    @property
    def current_captain(self) -> Player:
        if self.is_complete:
            raise RuntimeError("Draft complete: no current captain.")
        side = PICK_SEQUENCE[self.turn_index]
        return self.cap_a if side == "A" else self.cap_b

    def apply_pick(self, player: Player) -> DraftState:
        """Return a new state with `player` added to the current captain's team.

        Raises:
            ValueError if player is not in pool.
            RuntimeError if draft is already complete or cancelled.
        """
        if self.status != "picking":
            raise RuntimeError(f"Draft status={self.status}, cannot pick.")
        if player not in self.pool:
            raise ValueError(f"Player {player.id} not in the pool.")
        side = PICK_SEQUENCE[self.turn_index]
        new_pool = tuple(p for p in self.pool if p.id != player.id)
        if side == "A":
            new_team_a = (*self.team_a, player)
            new_team_b = self.team_b
        else:
            new_team_a = self.team_a
            new_team_b = (*self.team_b, player)
        new_turn = self.turn_index + 1
        new_status: DraftStatus = "complete" if new_turn >= len(PICK_SEQUENCE) else "picking"
        return replace(
            self,
            team_a=new_team_a,
            team_b=new_team_b,
            pool=new_pool,
            turn_index=new_turn,
            status=new_status,
        )


@dataclass(frozen=True)
class DraftResult:
    cap_a: Player
    cap_b: Player
    team_a: tuple[Player, ...]  # 5 players incl. cap_a
    team_b: tuple[Player, ...]  # 5 players incl. cap_b

    @classmethod
    def from_state(cls, state: DraftState) -> DraftResult:
        if state.status != "complete":
            raise ValueError(f"Draft not complete (status={state.status}).")
        return cls(
            cap_a=state.cap_a,
            cap_b=state.cap_b,
            team_a=state.team_a,
            team_b=state.team_b,
        )


class DraftCancelledError(Exception):
    """Raised when an admin cancels the draft via the button."""

    def __init__(self, reason: str, actor: Any | None = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor


def _is_admin(user: Any, role_names: tuple[str, ...]) -> bool:
    """Admin = Discord `manage_guild` permission OR a role whose name is
    in `role_names`. Aligns the "Cancel draft" button check with
    the rest of the codebase (`/match-cancel` etc.) where only `manage_guild`
    counts. The role-name fallback is kept for servers
    that have a "Match Staff" role without the elevated permission.
    """
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "manage_guild", False):
        return True
    return any(r.name in role_names for r in getattr(user, "roles", []))


def _build_player_lines(players: tuple[Player, ...]) -> str:
    if not players:
        return "_(vide)_"
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in players)


def _build_pool_lines(pool: tuple[Player, ...]) -> str:
    if not pool:
        return "_(vide)_"
    ordered = sorted(pool, key=lambda p: p.elo, reverse=True)
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in ordered)


def _build_sequence_marker(turn_index: int) -> str:
    """Show the ABBAABBA sequence with a cursor on the current pick."""
    parts = []
    for i, side in enumerate(PICK_SEQUENCE):
        if i == turn_index:
            parts.append(f"·{side}·")
        else:
            parts.append(side)
    return " ".join(parts)


class CaptainDraftSession:
    """Draft orchestration: posts the message, handles interactions,
    returns a DraftResult when the 8 picks are done (or raises
    DraftCancelledError if cancelled by an admin).
    """

    def __init__(
        self,
        *,
        prep_channel: Any,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
        admin_role_names: tuple[str, ...],
    ):
        self.prep_channel = prep_channel
        self.state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
        self.admin_role_names = admin_role_names
        self.message: Any | None = None
        self._lock = asyncio.Lock()
        # _done is created lazily inside run() to avoid calling
        # asyncio.get_event_loop() outside a coroutine (deprecated since Python 3.12+).
        self._done: asyncio.Future[DraftResult] | None = None

    async def run(self) -> DraftResult:
        """Block until the draft ends (complete OR cancelled).

        Returns: DraftResult si complete.
        Raises: DraftCancelledError if cancelled.
        """
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()

        embed = self._build_embed()
        view = self._build_view()
        content = (
            f"<@{self.state.cap_a.id}> <@{self.state.cap_b.id}> "
            f"- vous êtes les capitaines, c'est l'heure du draft !"
        )
        self.message = await self.prep_channel.send(content=content, embed=embed, view=view)
        logger.info(
            "[draft] init cap_a=%s cap_b=%s pool_size=%d",
            self.state.cap_a.id,
            self.state.cap_b.id,
            len(self.state.pool),
        )
        return await self._done

    def _build_embed(self) -> Any:
        import discord

        e = discord.Embed(
            title="🎯 [PRO] Draft des capitaines",
            color=discord.Color.gold(),
        )
        e.add_field(
            name=f"🅰️ Équipe 1 - Cap. <@{self.state.cap_a.id}>",
            value=_build_player_lines(self.state.team_a),
            inline=False,
        )
        e.add_field(
            name=f"🅱️ Équipe 2 - Cap. <@{self.state.cap_b.id}>",
            value=_build_player_lines(self.state.team_b),
            inline=False,
        )
        e.add_field(
            name="🎲 Joueurs disponibles",
            value=_build_pool_lines(self.state.pool),
            inline=False,
        )
        if self.state.is_complete:
            e.set_footer(text="✅ Draft terminé")
        elif self.state.status == "picking":
            cur = self.state.current_captain
            seq = _build_sequence_marker(self.state.turn_index)
            e.add_field(
                name=f"⏳ Au tour de <@{cur.id}> - choix #{self.state.turn_index + 1}",
                value=f"Séquence : {seq}",
                inline=False,
            )
        return e

    def _build_view(self) -> Any:
        import discord

        session = self  # capture for the callbacks

        class _View(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return await session._interaction_check(interaction)

        view = _View()
        if not self.state.is_complete and self.state.status == "picking":
            options = [
                discord.SelectOption(
                    label=p.name[:100],
                    description=f"{p.elo} ELO",
                    value=str(p.id),
                )
                for p in sorted(self.state.pool, key=lambda x: x.elo, reverse=True)
            ]
            select: discord.ui.Select[Any] = discord.ui.Select(
                custom_id="pro_draft_pick",
                placeholder="Choisissez votre joueur",
                min_values=1,
                max_values=1,
                options=options,
            )

            async def _select_cb(interaction: discord.Interaction) -> None:
                await session._on_pick(interaction)

            select.callback = _select_cb  # type: ignore[method-assign]
            view.add_item(select)

        cancel_btn: discord.ui.Button[Any] = discord.ui.Button(
            custom_id="pro_draft_cancel",
            style=discord.ButtonStyle.danger,
            label="❌ Annuler le draft",
            disabled=self.state.status != "picking",
        )

        async def _cancel_cb(interaction: discord.Interaction) -> None:
            await session._on_cancel(interaction)

        cancel_btn.callback = _cancel_cb  # type: ignore[method-assign]
        view.add_item(cancel_btn)
        return view

    async def _interaction_check(self, interaction: Any) -> bool:
        cid = interaction.data.get("custom_id", "")
        if cid == "pro_draft_pick":
            # Guard: if the draft is complete (late interaction on Discord's side),
            # reject cleanly rather than letting current_captain
            # raise a RuntimeError.
            if self.state.is_complete or interaction.user.id != self.state.current_captain.id:
                await interaction.response.send_message(
                    "⏳ Ce n'est pas votre tour.",
                    ephemeral=True,
                )
                return False
        elif cid == "pro_draft_cancel" and not _is_admin(
            interaction.user,
            self.admin_role_names,
        ):
            await interaction.response.send_message(
                "❌ Réservé aux admins.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_pick(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                # Interaction is stale (draft finished/cancelled in the meantime):
                # acknowledge silently to avoid the "Interaction failed" red banner.
                with contextlib.suppress(Exception):
                    await interaction.response.defer()
                return
            picked_id_str = interaction.data["values"][0]
            picked_id = int(picked_id_str)
            picked = next(
                (p for p in self.state.pool if p.id == picked_id),
                None,
            )
            if picked is None:
                await interaction.response.send_message(
                    "❌ Joueur déjà drafté.",
                    ephemeral=True,
                )
                return
            self.state = self.state.apply_pick(picked)
            logger.info(
                "[draft] pick turn=%d by=%s player=%s",
                self.state.turn_index - 1,
                interaction.user.id,
                picked_id,
            )
            embed = self._build_embed()
            view = self._build_view()
            # `interaction.response.edit_message` acknowledges AND edits in a
            # single API call: avoids the red "This interaction failed" that
            # captains saw when `message.edit` + a late defer
            # exceeded the 3s ACK window.
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except Exception:
                # Fallback if the response was already consumed (extreme
                # double-click case). Force the correct state via message.edit.
                logger.exception(
                    "[draft] edit_message via interaction raised, fallback message.edit"
                )
                if self.message is not None:
                    with contextlib.suppress(Exception):
                        await self.message.edit(embed=embed, view=view)
            if self.state.is_complete and self._done is not None and not self._done.done():
                self._done.set_result(DraftResult.from_state(self.state))

    async def _on_cancel(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                with contextlib.suppress(Exception):
                    await interaction.response.defer()
                return
            self.state = replace(self.state, status="cancelled")
            actor = interaction.user
            embed = self._build_embed()
            embed.title = "❌ Draft annulé"
            embed.description = f"Annulé par <@{actor.id}>"
            view = self._build_view()
            # Same pattern as `_on_pick`: edit_message acknowledges in the
            # same API call. Without it the "Cancel" button showed
            # "Interaction failed" to the admin even though the cancel went through.
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except Exception:
                logger.exception(
                    "[draft] edit_message via interaction raised, fallback message.edit"
                )
                if self.message is not None:
                    with contextlib.suppress(Exception):
                        await self.message.edit(embed=embed, view=view)
            logger.info("[draft] cancelled by=%s", actor.id)
            if self._done is not None and not self._done.done():
                self._done.set_exception(DraftCancelledError("admin", actor))
