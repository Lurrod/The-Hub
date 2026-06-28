"""
Cog V2 : queues 10mans avec boutons persistants (Rejoindre / Quitter).

3 queues simultanees par guild :
  - Pro Queue : reserve aux joueurs avec le role "Rank S | Pro Queue".
  - Open Queue : sans gate de role.
  - GC Queue : reserve aux joueurs avec le role "GC".

Invariants :
  - Un joueur ne peut etre que dans UNE queue a la fois (single-queue lock).
  - Chaque queue a son salon vocal "Waiting Room" dedie.
  - Les custom_ids des boutons portent le `queue_type` pour permettre la
    cohabitation des 3 messages persistants apres restart du bot.

Flux :
  1. Admin lance /setup-queue queue:<Pro|Open|GC> dans un salon -> message
     persistant pose pour ce type.
  2. Joueurs cliquent "Rejoindre" / "Quitter".
     - Refus si pas de compte Riot lie.
     - Refus si deja dans un match en cours.
     - Refus si deja dans une autre queue.
     - Refus si role gate non satisfait.
  3. A 10 joueurs : status passe a "forming", _on_full() est appele avec
     `queue_type` pour permettre au cog match de propager l'info.
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


# Roles "Match #1", "Match #2", "Match #3", "Match #4", "Match #5" attribues a un joueur en cours
# de match. Tant qu'un joueur a un de ces roles, il est dans un match
logger = logging.getLogger(__name__)


# ── Constantes par queue_type ─────────────────────────────────────
# Salons vocaux "Waiting Room" dedies par queue.
WAITING_ROOM_NAMES: dict[str, str] = {
    "pro": "Waiting Room Pro",
    "open": "Waiting Room Open",
    "gc": "Waiting Room GC",
}

# Roles autorises pour rejoindre une queue gated (n'importe lequel suffit).
# None = pas de gate.
QUEUE_ROLE_GATES: dict[str, tuple[str, ...] | None] = {
    "pro": ("Rank S | Pro Queue",),
    "open": None,
    "gc": ("GC",),
}

# Nom du salon textuel attendu pour chaque queue (utilise par /setup
# pour pre-poster les messages dans les bons salons).
QUEUE_CHANNEL_NAMES: dict[str, str] = {
    "pro": "pro-queue",
    "open": "open-queue",
    "gc": "gc-queue",
}

# Label affiche dans le titre de l'embed.
QUEUE_LABELS: dict[str, str] = {
    "pro": "Pro Queue",
    "open": "Open Queue",
    "gc": "GC Queue",
}

QUEUE_ROLE_NAME: str = "En Queue"  # role global, partage entre les 3 queues
QUEUE_SIZE: int = 10


# ── Roles helpers (inchanges) ─────────────────────────────────────
async def _grant_queue_role(member: discord.Member) -> str | None:
    role = discord.utils.get(member.guild.roles, name=QUEUE_ROLE_NAME)
    if role is None:
        return f"⚠️ Role **{QUEUE_ROLE_NAME}** introuvable sur le serveur."
    if role in member.roles:
        return None
    try:
        await member.add_roles(role, reason="Joined queue")
    except discord.Forbidden:
        return f"⚠️ Permissions insuffisantes pour ajouter le role **{QUEUE_ROLE_NAME}**."
    except discord.HTTPException:
        return None
    return None


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
    """Deplace `member` dans le salon vocal "Waiting Room <type>" si possible.

    Retourne un message d'info pour le joueur, ou None si tout s'est bien passe
    silencieusement. Discord n'autorise le deplacement que si le membre est deja
    connecte a un salon vocal du serveur.
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


# ── View persistante ──────────────────────────────────────────────
_LOCKS_MAXSIZE: int = 128


# Types de retour internes pour decouper `_join_callback` :
#   _JoinFailure  -> motif d'echec (message ephemeral a renvoyer au joueur)
#   _JoinSuccess  -> slot acquis, queue_doc a jour, drapeau de queue pleine
@dataclass(frozen=True)
class _JoinFailure:
    message: str


@dataclass(frozen=True)
class _JoinSuccess:
    queue_doc: dict
    full: bool


_JoinResult = _JoinFailure | _JoinSuccess


class QueueView(discord.ui.View):
    """View persistante par `queue_type`. Custom IDs distincts pour cohabiter.

    Les boutons sont crees manuellement (pas via `@discord.ui.button`)
    parce que le `custom_id` doit dependre de `queue_type` connu au
    runtime, pas du decorateur fige a l'import du module.
    """

    def __init__(self, db, queue_type: str, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.queue_type = queue_type
        self._on_full = on_full
        # OrderedDict + LRU bornee pour eviter une fuite memoire sur bot
        # multi-guilds longue duree (1 Lock par guild_id, jamais purge).
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
        # Refs fortes sur les tasks de formation de match (`_safe_on_full`).
        # Sans ca, Python peut GC la task avant qu'elle finisse
        # (cf. docs asyncio.create_task : "Save a reference to the result
        # of this function, to avoid a task disappearing mid-execution").
        # Le `done_callback` discard l'entree au terme de la task pour
        # eviter la fuite memoire sur bot longue duree.
        self._bg_tasks: set[asyncio.Task[None]] = set()

        # Boutons a custom_id dynamique (per-instance).
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
        """Verifie le gate de role pour cette queue.

        Returns:
            (True, None) si la queue n'a pas de gate (open).
            (True, role_name) si le gate est satisfait.
            (False, role_name) si le gate n'est pas satisfait. Le role_name
            est utilise par le caller pour le message d'erreur.
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
        # Acquitte tout de suite : sous contention du lock par-guild, le token
        # d'interaction (3s) peut expirer avant qu'on reponde -> 10062.
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

    # ── helpers _join_callback ───────────────────────────────────
    def _pre_lock_checks(self, inter: discord.Interaction) -> str | None:
        """Validations synchrones hors lock (type membre + role gate).

        Retourne le message d'erreur ephemeral a envoyer, ou None si tout
        est OK et que l'appelant peut acquerir le lock + interroger la BDD.
        Pure : pas d'I/O DB ni Discord ici, juste de la logique sur l'objet
        Interaction. Testable sans mongomock ni dpytest.
        """
        if not isinstance(inter.user, discord.Member):
            return "❌ Interaction invalide (hors serveur ou type d'utilisateur inattendu)."
        ok, required = self._has_required_role(inter.user)
        if not ok:
            return (
                f"❌ Cette queue est reservee aux joueurs avec le role "
                f"**{required}**."
            )
        return None

    async def _acquire_slot_under_lock(self, inter: discord.Interaction) -> _JoinResult:
        """Toute la phase BDD sous le lock par-guild.

        Couvre : lecture compte Riot + queue courante, insert atomique,
        fermeture queue pleine. Renvoie un `_JoinSuccess(queue_doc, full)`
        ou un `_JoinFailure(message)` que l'appelant pousse en ephemeral.
        Le lock est relache a la sortie de cette methode : les
        side-effects Discord (VC move, role grant, edit message) tournent
        ensuite sans serialisation.
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
                return _JoinFailure("❌ Lie d'abord ton compte Riot avec `/link-riot Pseudo#TAG`.")
            if current is not None and current != self.queue_type:
                return _JoinFailure(
                    f"❌ Tu es deja dans la queue **{current.upper()}**. "
                    "Quitte-la d'abord pour rejoindre une autre queue."
                )

            # Gate anti-doublon : refuse si le joueur est encore engage dans un
            # match dont la categorie Discord n'a pas ete supprimee (pending,
            # validated_*, contested et ELO non applique). Sans cette garde, un
            # joueur en plein match pourrait remplir une seconde queue et
            # demarrer un 2e match en parallele. Skip sur re-click idempotent
            # (`current == self.queue_type`) : impossible logiquement et evite
            # une requete Mongo inutile.
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
                        f"❌ Tu es deja dans un match en cours{suffix}. "
                        "Termine le vote ou demande l'annulation a un admin."
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
                # find_one_and_update renvoie le doc mis a jour : 1 round-trip.
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
        """Phase hors-lock : edit du message, VC move, role grant,
        confirmation ephemeral, declenchement formation si full.

        Toutes les ops Discord tournent en parallele (gather avec
        return_exceptions=True). Une erreur Discord sur l'une n'impacte
        pas les autres.
        """
        embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
        edit_task = inter.edit_original_response(embed=embed, view=self)
        results = await asyncio.gather(
            _move_to_waiting_room(inter.user, self.queue_type),
            _grant_queue_role(inter.user),
            edit_task,
            return_exceptions=True,
        )
        move_notice = results[0] if not isinstance(results[0], BaseException) else None
        role_notice = results[1] if not isinstance(results[1], BaseException) else None
        if isinstance(results[2], BaseException):
            logger.warning(
                "[queue_v2] edit_original_response a echoue: %r",
                results[2],
            )

        count = len(queue_doc.get("players", []))
        label = QUEUE_LABELS[self.queue_type]
        confirm = f"✅ Tu as rejoint la queue **{label}** ({count}/{QUEUE_SIZE})"
        if move_notice:
            confirm += f"\n{move_notice}"
        if role_notice:
            confirm += f"\n{role_notice}"
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
        """Invoque `_on_full` en garantissant la liberation de la queue
        en cas d'exception non capturee, sinon la queue reste en status
        'forming' et bloque toute nouvelle entree."""
        try:
            await self._on_full(inter, queue_doc, self.queue_type)
        except Exception:
            # On NE PROPAGE PAS le repr de l'exception aux utilisateurs :
            # certaines exceptions pymongo/Discord leakent noms de
            # collections, hosts, ou tokens partiels (CWE-209). Stack
            # complete dans les logs admin.
            logger.exception("[queue_v2] _safe_on_full a leve")
            try:
                repository.delete_active_queue(
                    self.db,
                    inter.guild_id,
                    self.queue_type,
                )
            except Exception:
                logger.exception("[queue_v2] cleanup apres on_full a leve")
            user_msg = (
                "❌ Erreur interne lors de la formation du match. "
                f"La queue {self.queue_type.upper()} a ete liberee, "
                "retentez avec /setup-queue."
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
                                "[queue_v2] DM fallback bloque (Forbidden) pour user %s",
                                inter.user.id,
                            )
            except Exception:
                logger.exception("[queue_v2] notification erreur a leve")

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
            # Lecture cross-queues pendant qu'on tient encore le lock pour
            # garantir la coherence : "le joueur est-il encore quelque part ?"
            still_in = None
            if isinstance(inter.user, discord.Member):
                still_in = await asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db,
                    inter.guild_id,
                    inter.user.id,
                )

        # Lock libere : side-effects Discord en parallele.
        embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
        tasks: list = [inter.edit_original_response(embed=embed, view=self)]
        if isinstance(inter.user, discord.Member) and still_in is None:
            tasks.append(_revoke_queue_role(inter.user))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("[queue_v2] leave side-effect a echoue: %r", r)


def _join_error_message(reason: str) -> str:
    return {
        "no_queue": "❌ Aucune queue active sur ce serveur.",
        "queue_closed": "❌ La queue est fermee (match en cours de formation).",
        "already_in": "❌ Tu es deja dans la queue.",
        "queue_full": "❌ La queue est pleine (10/10).",
        "race": "⚠️ Conflit, reessaie.",
    }.get(reason, f"❌ Erreur : {reason}")


def _leave_error_message(reason: str) -> str:
    return {
        "no_queue": "❌ Aucune queue active.",
        "not_in": "❌ Tu n'es pas dans la queue.",
    }.get(reason, f"❌ Erreur : {reason}")


# ── Cog ───────────────────────────────────────────────────────────
_QUEUE_CHOICES = [
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
]


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, on_full=None) -> None:
        self.bot = bot
        self.db = db
        self.on_full = on_full
        # 1 view par queue_type, custom_ids distincts. Toutes branchees
        # sur le meme on_full callback (le cog match dispatchera selon
        # le queue_type passe a _safe_on_full).
        self.views: dict[str, QueueView] = {
            qt: QueueView(db, queue_type=qt, on_full=on_full) for qt in repository.QUEUE_TYPES
        }

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Quand un joueur quitte le serveur (kick, ban, leave), le retirer
        des queues actives (toutes, on ne sait pas dans laquelle il etait).
        Sans ce handler, sa place reste reservee et la queue se bloque a
        9/10 jusqu'a ce qu'un admin force un reset."""
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
                logger.exception("[queue_v2] on_member_remove a leve (qt=%s)", qt)

    @app_commands.command(
        name="setup-queue",
        description="Pose le message de queue dans ce salon",
    )
    @app_commands.describe(queue="Type de queue : Pro, Open ou GC")
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
                f"🚫 La queue **{queue.upper()}** doit etre configuree dans #{expected_channel}.",
                ephemeral=True,
            )
            return

        # Reset de la queue precedente du meme type s'il y en avait une
        repository.delete_active_queue(self.db, interaction.guild_id, queue)

        await self.post_queue_message(interaction.channel, queue)

        await interaction.response.send_message(
            f"✅ Queue **{queue.upper()}** active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    async def post_queue_message(
        self,
        channel: discord.TextChannel,
        queue_type: str,
    ) -> None:
        """Pose un nouveau message de queue dans `channel` et l'enregistre.

        Utilise par /setup-queue ET par le cog match apres formation
        d'un match (pour qu'une nouvelle queue soit immediatement
        disponible apres formation)."""
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
        description="Ferme la queue active d'un type",
    )
    @app_commands.describe(queue="Type de queue : Pro, Open ou GC")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def close_queue(
        self,
        interaction: discord.Interaction,
        queue: str,
    ) -> None:
        # Recupere la queue active pour pouvoir supprimer le message
        # persistant Rejoindre/Quitter dans Discord avant la purge DB,
        # et capturer la liste des joueurs avant de purger.
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

        # Apres la purge DB, retirer le role "En Queue" a chaque joueur
        # qui n'est plus dans aucune autre queue active. Le check
        # `find_player_in_any_queue` garantit qu'on ne strip pas le role
        # a un joueur encore present dans une autre queue (un joueur ne
        # peut techniquement etre que dans une seule, mais on reste safe).
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
                            "[queue_v2] close-queue revoke role a echoue: %r",
                            r,
                        )

        msg = (
            f"✅ Queue {queue.upper()} supprimee."
            if deleted
            else f"ℹ️ Aucune queue {queue.upper()} active."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @setup_queue.error
    @close_queue.error
    async def _perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserve aux administrateurs.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db, on_full=None) -> None:
    cog = QueueCog(bot, db, on_full=on_full)
    await bot.add_cog(cog)
    # Enregistre les 3 views pour qu'elles persistent apres restart.
    for view in cog.views.values():
        bot.add_view(view)
