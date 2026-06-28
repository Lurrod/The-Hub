"""
Cog V2 : files 10mans avec boutons persistants (Rejoindre / Quitter).

2 files simultanées par serveur :
  - File Open : ouverte à tous (aucune restriction de rôle).
  - File Advanced : réservée aux joueurs ayant le rôle "Rank Q | Advanced Queue".

Invariants :
  - Un joueur ne peut être que dans UNE seule file à la fois (verrou mono-file).
  - Chaque file a son salon vocal "Waiting Room" dédié.
  - Les custom_ids des boutons incluent le `queue_type` afin que les
    messages persistants puissent coexister après un redémarrage du bot.

Déroulement :
  1. Un admin lance /setup-queue queue:<Open|Advanced> dans un salon ->
     un message persistant est posté pour ce type.
  2. Les joueurs cliquent sur "Rejoindre" / "Quitter".
     - Refusé si aucun compte Riot n'est lié.
     - Refusé si déjà dans un match en cours.
     - Refusé si déjà dans une autre file.
     - Refusé si le rôle requis n'est pas satisfait.
  3. À 10 joueurs : le statut passe à "forming", _on_full() est appelé
     avec `queue_type` pour laisser le cog de match propager l'info.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

# Roles "Match #1", "Match #2", "Match #3", "Match #4", "Match #5" are assigned to a
# player currently in a match. As long as a player has one of these roles, they are in a match.
logger = logging.getLogger(__name__)


# ── Per queue_type constants ──────────────────────────────────────
# Dedicated "Waiting Room" voice channels per queue.
WAITING_ROOM_NAMES: dict[str, str] = {
    "open": "Waiting Room Open",
    "advanced": "Waiting Room Advanced",
    "draft": "Waiting Room Draft",
}

# Allowed roles to join a gated queue (any one of them is enough).
# None = no gate.
QUEUE_ROLE_GATES: dict[str, tuple[str, ...] | None] = {
    "open": None,
    "advanced": ("Rank Q | Advanced Queue",),
    "draft": ("Draft",),
}

# Expected text channel name for each queue (used by /setup to
# pre-post messages in the right channels).
QUEUE_CHANNEL_NAMES: dict[str, str] = {
    "open": "open-queue",
    "advanced": "advanced-queue",
    "draft": "draft-queue",
}

# Label displayed in the embed title.
QUEUE_LABELS: dict[str, str] = {
    "open": "Open Queue",
    "advanced": "Advanced Queue",
    "draft": "Draft Queue",
}

QUEUE_ROLE_NAME: str = "En Queue"  # global role, shared between all queues
QUEUE_SIZE: int = 10


# ── Role helpers (unchanged) ──────────────────────────────────────
async def _revoke_queue_role(member: discord.Member) -> None:
    role = discord.utils.get(member.guild.roles, name=QUEUE_ROLE_NAME)
    if role is None or role not in member.roles:
        return
    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
        await member.remove_roles(role, reason="Left queue")


async def _move_to_waiting_room(
    member: discord.Member,
    queue_type: str,
) -> str | None:
    """Move `member` into the "Waiting Room <type>" voice channel if possible.

    Returns an info message for the player, or None if everything went
    silently well. Discord only allows moving a member who is already
    connected to a voice channel on the server.
    """
    waiting_name = WAITING_ROOM_NAMES[queue_type]
    waiting = discord.utils.get(member.guild.voice_channels, name=waiting_name)
    if waiting is None:
        return None

    voice_state = member.voice
    if voice_state is None or voice_state.channel is None:
        return f"ℹ️ Connecte-toi a un salon vocal pour etre deplace dans **{waiting_name}**."

    if voice_state.channel.id == waiting.id:
        return None

    try:
        await member.move_to(waiting, reason=f"Auto-move queue join ({queue_type})")
    except discord.Forbidden:
        return f"⚠️ Permissions insuffisantes pour te deplacer dans **{waiting_name}**."
    except discord.HTTPException:
        return None
    return None


# ── Embed builder ─────────────────────────────────────────────────
def build_queue_embed(
    queue_doc: dict | None,
    guild: discord.Guild,
    queue_type: str,
) -> discord.Embed:
    label = QUEUE_LABELS[queue_type]
    players = list((queue_doc or {}).get("players", []))
    count = len(players)
    full = count >= QUEUE_SIZE
    status = (queue_doc or {}).get("status", "open")

    if status == "forming":
        color = 0xE67E22
        state = "🔥 Match en formation"
    elif full:
        color = 0x2ECC71
        state = "🟢 Queue pleine !"
    else:
        color = 0x5865F2
        state = "🔵 En attente de joueurs"

    embed = discord.Embed(
        title=f"🎮 {label} 10mans - {count}/{QUEUE_SIZE}",
        description=state,
        color=color,
        timestamp=datetime.now(UTC),
    )

    if players:
        mentions = "\n".join(f"• <@{uid}>" for uid in players)
        embed.add_field(name="Joueurs", value=mentions, inline=False)
    else:
        embed.add_field(name="Joueurs", value="*Personne pour le moment.*", inline=False)

    embed.set_footer(text=guild.name)
    return embed


# ── Persistent view ───────────────────────────────────────────────
_LOCKS_MAXSIZE: int = 128


# Internal return types to split up `_join_callback`:
#   _JoinFailure  -> failure reason (ephemeral message to send to the player)
#   _JoinSuccess  -> slot acquired, updated queue_doc, full-queue flag
@dataclass(frozen=True)
class _JoinFailure:
    message: str


@dataclass(frozen=True)
class _JoinSuccess:
    queue_doc: dict
    full: bool


_JoinResult = _JoinFailure | _JoinSuccess


class QueueView(discord.ui.View):
    """Persistent view per `queue_type`. Distinct custom IDs to coexist.

    Buttons are created manually (not via `@discord.ui.button`)
    because the `custom_id` must depend on `queue_type` known at
    runtime, not on the decorator frozen at module import time.
    """

    def __init__(self, db, queue_type: str, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.queue_type = queue_type
        self._on_full = on_full
        # OrderedDict + bounded LRU to avoid a memory leak on a long-running
        # multi-guild bot (1 Lock per guild_id, never purged).
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
        # Strong refs on match-formation tasks (`_safe_on_full`).
        # Without this, Python may GC the task before it finishes
        # (cf. asyncio.create_task docs: "Save a reference to the result
        # of this function, to avoid a task disappearing mid-execution").
        # The `done_callback` discards the entry at task completion to
        # avoid a memory leak on a long-running bot.
        self._bg_tasks: set[asyncio.Task[None]] = set()

        # Buttons with dynamic custom_id (per-instance).
        join: discord.ui.Button = discord.ui.Button(
            label="Rejoindre",
            style=discord.ButtonStyle.success,
            custom_id=f"queue_v2:join:{queue_type}",
        )
        join.callback = self._join_callback
        self.join_btn = join
        self.add_item(join)

        leave: discord.ui.Button = discord.ui.Button(
            label="Quitter",
            style=discord.ButtonStyle.danger,
            custom_id=f"queue_v2:leave:{queue_type}",
        )
        leave.callback = self._leave_callback
        self.leave_btn = leave
        self.add_item(leave)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
            while len(self._locks) > _LOCKS_MAXSIZE:
                self._locks.popitem(last=False)
        else:
            self._locks.move_to_end(guild_id)
        return lock

    def _has_required_role(
        self,
        member: discord.Member,
    ) -> tuple[bool, str | None]:
        """Check the role gate for this queue.

        Returns:
            (True, None) if the queue has no gate (open).
            (True, role_name) if the gate is satisfied.
            (False, role_name) if the gate is not satisfied. The role_name
            is used by the caller for the error message.
        """
        required = QUEUE_ROLE_GATES.get(self.queue_type)
        if required is None:
            return True, None
        label = " ou ".join(required)
        member_role_names = {r.name for r in member.roles}
        if any(name in member_role_names for name in required):
            return True, label
        return False, label

    async def _join_callback(self, inter: discord.Interaction):
        # Acknowledge immediately: under per-guild lock contention, the
        # interaction token (3s) may expire before we reply -> 10062.
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        pre_check_err = self._pre_lock_checks(inter)
        if pre_check_err is not None:
            await inter.followup.send(pre_check_err, ephemeral=True)
            return

        result = await self._acquire_slot_under_lock(inter)
        if isinstance(result, _JoinFailure):
            await inter.followup.send(result.message, ephemeral=True)
            return

        await self._broadcast_join_side_effects(inter, result.queue_doc, result.full)

    # ── _join_callback helpers ───────────────────────────────────
    def _pre_lock_checks(self, inter: discord.Interaction) -> str | None:
        """Synchronous validations outside the lock (member type + role gate).

        Returns the ephemeral error message to send, or None if everything
        is OK and the caller can acquire the lock + query the DB.
        Pure: no DB or Discord I/O here, just logic on the Interaction
        object. Testable without mongomock or dpytest.
        """
        if not isinstance(inter.user, discord.Member):
            return "❌ Interaction invalide (hors d'un serveur ou type d'utilisateur inattendu)."
        ok, required = self._has_required_role(inter.user)
        if not ok:
            return f"❌ Cette file est réservée aux joueurs ayant le rôle **{required}**."
        return None

    async def _acquire_slot_under_lock(self, inter: discord.Interaction) -> _JoinResult:
        """All DB phase under the per-guild lock.

        Covers: Riot account read + current queue read, atomic insert,
        queue-full close. Returns `_JoinSuccess(queue_doc, full)` or
        `_JoinFailure(message)` that the caller sends as ephemeral.
        The lock is released on exit from this method: Discord
        side-effects (VC move, role grant, message edit) then run
        without serialization.
        """
        async with self._lock(inter.guild_id):
            riot, current = await asyncio.gather(
                asyncio.to_thread(
                    repository.get_riot_account,
                    self.db,
                    inter.user.id,
                ),
                asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db,
                    inter.guild_id,
                    inter.user.id,
                ),
            )
            if not riot:
                return _JoinFailure("❌ Lie d'abord ton compte Riot avec `/link-riot Name#TAG`.")
            if current is not None and current != self.queue_type:
                return _JoinFailure(
                    f"❌ Tu es déjà dans la file **{current.upper()}**. "
                    "Quitte-la d'abord pour rejoindre une autre file."
                )

            # Anti-duplicate gate: refuse if the player is still engaged in a
            # match whose Discord category has not been deleted (pending,
            # validated_*, contested and ELO not applied). Without this
            # guard, a player in an ongoing match could fill a second queue
            # and start a 2nd parallel match. Skip on idempotent re-click
            # (`current == self.queue_type`): impossible logically and
            # avoids an unnecessary Mongo query.
            if current != self.queue_type:
                active_match = await asyncio.to_thread(
                    repository.find_active_match_for_player,
                    self.db,
                    inter.user.id,
                )
                if active_match is not None:
                    match_num = active_match.get("match_number")
                    suffix = f" (**Match #{match_num}**)" if match_num else ""
                    return _JoinFailure(
                        f"❌ Tu es déjà dans un match en cours{suffix}. "
                        "Termine le vote ou demande à un admin de l'annuler."
                    )

            res = await asyncio.to_thread(
                repository.add_player_to_queue,
                self.db,
                inter.guild_id,
                self.queue_type,
                inter.user.id,
            )
            if not res.success:
                return _JoinFailure(_join_error_message(res.reason))

            queue_doc = res.queue
            full = len(queue_doc.get("players", [])) >= QUEUE_SIZE
            if full:
                # find_one_and_update returns the updated doc: 1 round-trip.
                closed = await asyncio.to_thread(
                    repository.close_active_queue,
                    self.db,
                    inter.guild_id,
                    self.queue_type,
                )
                if closed is not None:
                    queue_doc = closed
            return _JoinSuccess(queue_doc=queue_doc, full=full)

    async def _broadcast_join_side_effects(
        self,
        inter: discord.Interaction,
        queue_doc: dict,
        full: bool,
    ) -> None:
        """Lock-free phase: message edit, VC move, role grant,
        ephemeral confirmation, formation trigger if full.

        All Discord ops run in parallel (gather with
        return_exceptions=True). A Discord error on one does not
        impact the others.
        """
        embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
        edit_task = inter.edit_original_response(embed=embed, view=self)
        results = await asyncio.gather(
            _move_to_waiting_room(inter.user, self.queue_type),
            edit_task,
            return_exceptions=True,
        )
        move_notice = results[0] if not isinstance(results[0], BaseException) else None
        if isinstance(results[1], BaseException):
            logger.warning(
                "[queue_v2] edit_original_response failed: %r",
                results[1],
            )

        count = len(queue_doc.get("players", []))
        label = QUEUE_LABELS[self.queue_type]
        confirm = f"✅ Tu as rejoint la file **{label}** ({count}/{QUEUE_SIZE})"
        if move_notice:
            confirm += f"\n{move_notice}"
        await inter.followup.send(confirm, ephemeral=True)

        if full and self._on_full:
            task = asyncio.create_task(self._safe_on_full(inter, queue_doc))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _safe_on_full(
        self,
        inter: discord.Interaction,
        queue_doc: dict,
    ) -> None:
        """Invoke `_on_full` ensuring the queue is released on uncaught
        exception, otherwise the queue stays in 'forming' status and
        blocks any new entries."""
        try:
            await self._on_full(inter, queue_doc, self.queue_type)
        except Exception:
            # We do NOT propagate the exception repr to users:
            # some pymongo/Discord exceptions leak collection names,
            # hosts, or partial tokens (CWE-209). Full stack in admin
            # logs.
            logger.exception("[queue_v2] _safe_on_full raised")
            try:
                repository.delete_active_queue(
                    self.db,
                    inter.guild_id,
                    self.queue_type,
                )
            except Exception:
                logger.exception("[queue_v2] cleanup after on_full raised")
            user_msg = (
                "❌ Erreur interne lors de la formation du match. "
                f"La file {self.queue_type.upper()} a été libérée, "
                "réessaie avec /setup-queue."
            )
            channel = inter.channel
            try:
                if channel is not None:
                    await channel.send(user_msg)
                else:
                    logger.warning(
                        "[queue_v2] inter.channel is None, fallback DM to user %s in guild %s",
                        inter.user.id,
                        inter.guild_id,
                    )
                    if inter.user is not None:
                        try:
                            await inter.user.send(user_msg)
                        except discord.Forbidden:
                            logger.warning(
                                "[queue_v2] DM fallback blocked (Forbidden) for user %s",
                                inter.user.id,
                            )
            except Exception:
                logger.exception("[queue_v2] error notification raised")

    async def _leave_callback(self, inter: discord.Interaction):
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            res = await asyncio.to_thread(
                repository.remove_player_from_queue,
                self.db,
                inter.guild_id,
                self.queue_type,
                inter.user.id,
            )
            if not res.success:
                await inter.followup.send(
                    _leave_error_message(res.reason),
                    ephemeral=True,
                )
                return
            queue_doc = res.queue
            # Cross-queue read while we still hold the lock to guarantee
            # consistency: "is the player still somewhere ?"
            still_in = None
            if isinstance(inter.user, discord.Member):
                still_in = await asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db,
                    inter.guild_id,
                    inter.user.id,
                )

        # Lock released: Discord side-effects in parallel.
        embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
        tasks: list = [inter.edit_original_response(embed=embed, view=self)]
        if isinstance(inter.user, discord.Member) and still_in is None:
            tasks.append(_revoke_queue_role(inter.user))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("[queue_v2] leave side-effect failed: %r", r)


def _join_error_message(reason: str) -> str:
    return {
        "no_queue": "❌ Aucune file active sur ce serveur.",
        "queue_closed": "❌ La file est fermée (match en cours de formation).",
        "already_in": "❌ Tu es déjà dans la file.",
        "queue_full": "❌ La file est pleine (10/10).",
        "race": "⚠️ Conflit, réessaie.",
    }.get(reason, f"❌ Erreur : {reason}")


def _leave_error_message(reason: str) -> str:
    return {
        "no_queue": "❌ Aucune file active.",
        "not_in": "❌ Tu n'es pas dans la file.",
    }.get(reason, f"❌ Erreur : {reason}")


# ── Cog ───────────────────────────────────────────────────────────
_QUEUE_CHOICES = [
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="Advanced", value="advanced"),
    app_commands.Choice(name="Draft", value="draft"),
]


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, on_full=None) -> None:
        self.bot = bot
        self.db = db
        self.on_full = on_full
        # 1 view per queue_type, distinct custom_ids. All wired to the
        # same on_full callback (the match cog will dispatch by the
        # queue_type passed to _safe_on_full).
        self.views: dict[str, QueueView] = {
            qt: QueueView(db, queue_type=qt, on_full=on_full) for qt in repository.QUEUE_TYPES
        }

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """When a player leaves the server (kick, ban, leave), remove them
        from active queues (all of them, we do not know which one they were in).
        Without this handler, their slot stays reserved and the queue gets
        stuck at 9/10 until an admin force-resets it."""
        for qt in repository.QUEUE_TYPES:
            try:
                await asyncio.to_thread(
                    repository.remove_player_from_queue,
                    self.db,
                    member.guild.id,
                    qt,
                    member.id,
                )
            except Exception:
                logger.exception("[queue_v2] on_member_remove raised (qt=%s)", qt)

    @app_commands.command(
        name="setup-queue",
        description="Poste le message de file dans ce salon",
    )
    @app_commands.describe(queue="Type de file : Open ou Advanced")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_queue(
        self,
        interaction: discord.Interaction,
        queue: str,
    ) -> None:
        expected_channel = QUEUE_CHANNEL_NAMES[queue]
        if getattr(interaction.channel, "name", None) != expected_channel:
            await interaction.response.send_message(
                f"🚫 La file **{queue.upper()}** doit être configurée dans #{expected_channel}.",
                ephemeral=True,
            )
            return

        # Reset of the previous queue of the same type if there was one
        repository.delete_active_queue(self.db, interaction.guild_id, queue)

        await self.post_queue_message(interaction.channel, queue)

        await interaction.response.send_message(
            f"✅ File **{queue.upper()}** active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    async def post_queue_message(
        self,
        channel: discord.TextChannel,
        queue_type: str,
    ) -> None:
        """Post a new queue message in `channel` and register it.

        Used by /setup-queue AND by the match cog after match formation
        (so a new queue is immediately available after formation)."""
        view = self.views[queue_type]
        embed = build_queue_embed(None, channel.guild, queue_type)
        msg = await channel.send(embed=embed, view=view)
        repository.setup_active_queue(
            self.db,
            guild_id=channel.guild.id,
            queue_type=queue_type,
            channel_id=channel.id,
            message_id=msg.id,
        )

    @app_commands.command(
        name="close-queue",
        description="Ferme la file active d'un type",
    )
    @app_commands.describe(queue="Type de file : Open ou Advanced")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def close_queue(
        self,
        interaction: discord.Interaction,
        queue: str,
    ) -> None:
        # Fetch the active queue to be able to delete the persistent
        # Join/Leave message in Discord before the DB purge, and to
        # capture the list of players before purging.
        queue_doc = repository.get_active_queue(
            self.db,
            interaction.guild_id,
            queue,
        )
        player_ids: list[int] = []
        if queue_doc is not None:
            player_ids = [int(uid) for uid in queue_doc.get("players", [])]
            channel = interaction.guild.get_channel(
                int(queue_doc["channel_id"]),
            )
            if channel is not None:
                try:
                    msg_obj = await channel.fetch_message(
                        int(queue_doc["message_id"]),
                    )
                    await msg_obj.delete()
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    pass

        deleted = repository.delete_active_queue(
            self.db,
            interaction.guild_id,
            queue,
        )

        # After the DB purge, remove the "In Queue" role from each player
        # who is no longer in any other active queue. The
        # `find_player_in_any_queue` check ensures we do not strip the
        # role from a player still present in another queue (a player
        # technically can only be in one, but stay safe).
        if player_ids:
            guild = interaction.guild
            role_tasks: list = []
            for uid in player_ids:
                member = guild.get_member(uid)
                if member is None:
                    continue
                still_in = await asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db,
                    guild.id,
                    uid,
                )
                if still_in is None:
                    role_tasks.append(_revoke_queue_role(member))
            if role_tasks:
                results = await asyncio.gather(*role_tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, BaseException):
                        logger.warning(
                            "[queue_v2] close-queue revoke role failed: %r",
                            r,
                        )

        msg = (
            f"✅ File {queue.upper()} supprimée."
            if deleted
            else f"ℹ️ Aucune file {queue.upper()} active."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @setup_queue.error
    @close_queue.error
    async def _perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Réservé aux administrateurs.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db, on_full=None) -> None:
    cog = QueueCog(bot, db, on_full=on_full)
    await bot.add_cog(cog)
    # Register the views so they persist after restart.
    for view in cog.views.values():
        bot.add_view(view)
