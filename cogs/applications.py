"""
Applications + welcome + report cog. Extracted from bot.py (monolith refactor).

Contains:
  - Application system (ApplicationModal, StaffModal, RefuseReasonModal,
    WelcomeView, ApplicationReviewView).
  - /welcome: posts one Apply button per queue tier (+ Coach button) in #verify.
  - /report: posts the ticket opening panel (TicketPanelView) with 2
    options in the current channel:
      * Reports -> ReportModal (anonymous report).
      * Ranks   -> RankModal (rank application, identified candidate).
  - _open_ticket_channel: creates the `ticket-{N}` channel (shared by Reports/Ranks).
  - CloseTicketView: closes a ticket.

All persistent views (stable custom_id) are registered via
`bot.add_view(...)` in `setup()`. Modals are instantiated on the fly.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services import repository

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────
CANDIDATURE_CHANNEL = "applications"
WELCOME_CHANNEL = "verify"
PLAYERS_ROLE = "Members"
STAFF_ROLE = "Coach/Analyst/Manager"
TICKETS_CATEGORY_NAME = "Tickets"
CANDIDATURE_COOLDOWN_SECONDS = 3600

# Player application queues: the single Advanced queue is gated by a role
# granted on accept. The Apply button on /welcome targets the Advanced
# queue, the modal carries it through the embed, and the gating role is
# auto-assigned when an admin clicks Accept.
# The Open queue is intentionally absent: it is UNGATED (no role required),
# so the welcome Open button grants no special queue role.
QUEUE_TIERS: dict[str, tuple[str, str]] = {
    "advanced": ("Advanced Queue", "Rank Q | Advanced Queue"),
}
QUEUE_TIER_FIELD_NAME = "🎯 File ciblée"


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Reproduces `bot.has_access` without circular dependency.

    Admin (manage_guild) OR bypass role configured via /bypass.
    """
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _try_acquire_candidature_cooldown(db, uid: str) -> tuple[bool, float]:
    """Atomically attempts to acquire an application cooldown slot.

    Resolves the read-then-write race: two concurrent submissions cannot
    both pass the check (CAS via conditional update + insert with
    DuplicateKeyError handling).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=CANDIDATURE_COOLDOWN_SECONDS)
    cooldown_col = db["candidature_cooldowns"]
    res = cooldown_col.update_one(
        {"_id": uid, "last_apply": {"$lt": cutoff}},
        {"$set": {"last_apply": now}},
    )
    if res.modified_count == 1:
        return True, 0.0
    try:
        cooldown_col.insert_one({"_id": uid, "last_apply": now})
        return True, 0.0
    except DuplicateKeyError:
        pass
    doc = cooldown_col.find_one({"_id": uid})
    if doc is None:
        return True, 0.0
    last = doc["last_apply"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    remaining = CANDIDATURE_COOLDOWN_SECONDS - (now - last).total_seconds()
    if remaining <= 0:
        return True, 0.0
    return False, remaining


def _parse_application_embed(
    message: discord.Message,
) -> tuple[int | None, str, bool, str | None]:
    """Extracts (applicant_id, username, is_staff, queue_tier) from an
    application embed.

    Allows `ApplicationReviewView` to be persistent (without internal state)
    by reconstructing the context from the message on each click.

    `queue_tier` is the QUEUE_TIERS key matching the embed's
    `QUEUE_TIER_FIELD_NAME` value, or None for:
      - staff applications (no queue),
      - legacy embeds (pre-queue-tier rollout),
      - unknown queue labels (safer than guessing).
    """
    if not message.embeds:
        return None, "", False, None
    embed = message.embeds[0]
    is_staff = "Staff" in (embed.title or "")
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "") if embed.footer else ""
    if footer_text.startswith("ID:"):
        try:
            applicant_id = int(footer_text.split(":", 1)[1].strip())
        except (ValueError, IndexError):
            applicant_id = None
    pseudo = ""
    queue_label: str | None = None
    for field in embed.fields:
        if field.name in ("🎮 Pseudo en jeu", "🎮 Pseudo"):
            pseudo = field.value or ""
        elif field.name == QUEUE_TIER_FIELD_NAME:
            queue_label = (field.value or "").strip()
    queue_tier: str | None = None
    if queue_label:
        for tier_key, (label, _role) in QUEUE_TIERS.items():
            if label == queue_label:
                queue_tier = tier_key
                break
    return applicant_id, pseudo, is_staff, queue_tier


# ── Modals ────────────────────────────────────────────────────────
class ApplicationModal(discord.ui.Modal, title="Candidature 10mans"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="Quel est ton pseudo ?",
        placeholder="Comment dois-je t'appeler ? ex. jetax",
        max_length=50,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Lien vers ton tracker", placeholder="https://tracker.gg/...", max_length=200
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Expérience tournois / LAN ?",
        placeholder="Liste les tournois/LAN auxquels tu as participé",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView, queue_tier: str) -> None:
        super().__init__()
        if queue_tier not in QUEUE_TIERS:
            raise ValueError(
                f"unknown queue_tier {queue_tier!r}; expected one of {list(QUEUE_TIERS)}"
            )
        self.db = db
        self.review_view = review_view
        self.queue_tier = queue_tier

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Candidature reçue !",
                    description="Merci pour ta candidature, nous étudions ton profil et reviendrons vers toi dès que possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon des candidatures introuvable.", ephemeral=True)
            return
        queue_label, _queue_role = QUEUE_TIERS[self.queue_tier]
        embed = discord.Embed(
            title="📋 Nouvelle candidature",
            description="🎮 **Candidature joueur**",
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo en jeu", value=self.pseudo.value, inline=True)
        embed.add_field(name=QUEUE_TIER_FIELD_NAME, value=queue_label, inline=True)
        embed.add_field(name="🔗 Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="🏆 Tournois / LAN",
            value=self.experience.value if self.experience.value else "Aucun",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=False,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class StaffModal(discord.ui.Modal, title="Candidature Staff"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="Quel est ton pseudo ?",
        placeholder="Comment dois-je t'appeler ? ex. jetax",
        max_length=50,
    )
    poste: discord.ui.TextInput = discord.ui.TextInput(
        label="Poste actuel",
        placeholder="ex. Coach, Analyste, Manager... et dans quelle structure/organisation ?",
        max_length=100,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Expérience",
        placeholder="Décris ton expérience dans le domaine...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__()
        self.db = db
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Candidature reçue !",
                    description="Merci pour ta candidature, nous étudions ton profil et reviendrons vers toi dès que possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon des candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 Nouvelle candidature Staff",
            description="🎯 **Candidature Coach / Analyste / Manager**",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo", value=self.pseudo.value, inline=True)
        embed.add_field(name="💼 Poste", value=self.poste.value, inline=False)
        embed.add_field(
            name="📋 Expérience",
            value=self.experience.value if self.experience.value else "Aucune",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=True,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class RefuseReasonModal(discord.ui.Modal, title="Raison du refus"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Raison du refus (facultatif)",
        placeholder="Explique pourquoi...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, applicant_id: int):
        super().__init__()
        self.db = db
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="refused",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ Cette candidature a déjà été traitée par un autre admin.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(self.applicant_id)
        reason_text = self.reason.value if self.reason.value else "Aucune raison fournie."
        if member:
            try:
                embed_dm = discord.Embed(
                    title="❌ Candidature refusée",
                    description="Désolé, ta candidature n'a pas été retenue. N'hésite pas à retenter ta chance plus tard.",
                    color=0xE74C3C,
                    timestamp=datetime.now(UTC),
                )
                embed_dm.add_field(name="📋 Raison", value=reason_text, inline=False)
                await member.send(embed=embed_dm)
            except discord.Forbidden:
                pass
        try:
            embed = interaction.message.embeds[0]
            embed.color = 0xE74C3C
            embed.add_field(name="Refusée par", value=interaction.user.mention, inline=True)
            embed.add_field(name="📋 Raison", value=reason_text, inline=True)
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)
        await interaction.followup.send("✅ Candidature refusée.", ephemeral=True)


async def _open_ticket_channel(
    interaction: discord.Interaction,
    db,
    *,
    member_access: discord.Member | None = None,
) -> discord.TextChannel | None:
    """Creates the `ticket-{N}` channel in the `Tickets` category.

    Shared by Reports and Queue Application tickets. Returns the created
    channel, or `None` if the operation fails (in that case the user has
    already received an ephemeral error message via `followup`). The caller
    must have deferred the interaction beforehand (`defer(..., thinking=True)`).

    If `member_access` is provided (e.g. Queue Application, where the
    candidate is identified), the channel inherits overwrites copied from
    the category + read/write access for this member, so they can chat with
    staff in THEIR ticket. Without `member_access` (e.g. anonymous Reports),
    the channel stays synced with the category: the creator has no explicit
    access and anonymity is preserved.
    """
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "❌ Cette commande doit être utilisée dans un serveur.",
            ephemeral=True,
        )
        return None

    category = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if category is None:
        try:
            category = await guild.create_category(TICKETS_CATEGORY_NAME)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Le bot n'a pas la permission **Gérer les salons** pour "
                f"créer la catégorie `{TICKETS_CATEGORY_NAME}`.",
                ephemeral=True,
            )
            return None

    # The counter is incremented BEFORE the channel is created: if creation
    # fails (Forbidden), the number is "consumed" and a gap will remain in
    # the ticket numbering. This is intentionally tolerated - gaps in ticket
    # numbers are harmless and avoid fragile rollback logic.
    counter_doc = db["ticket_counters"].find_one_and_update(
        {"_id": str(guild.id)},
        {"$inc": {"counter": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    next_number = int(counter_doc["counter"])
    channel_name = f"ticket-{next_number}"

    # For an identified ticket (Queue Application), we copy the category's
    # overwrites to preserve its config (staff / @everyone) and then add
    # dedicated access for the candidate. Without `member_access`, we let
    # the channel sync with the category (behavior of anonymous Reports).
    create_kwargs: dict = {"category": category}
    if member_access is not None:
        overwrites = dict(category.overwrites)
        overwrites[member_access] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
        create_kwargs["overwrites"] = overwrites

    try:
        return await guild.create_text_channel(channel_name, **create_kwargs)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Le bot n'a pas la permission de créer le salon de ticket.",
            ephemeral=True,
        )
        return None


class ReportModal(discord.ui.Modal, title="Envoyer un signalement anonyme"):
    target: discord.ui.TextInput = discord.ui.TextInput(
        label="Qui signales-tu ?",
        placeholder="Pseudo Discord / @mention / ID du joueur",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    queue: discord.ui.TextInput = discord.ui.TextInput(
        label="Dans quelle file ?",
        placeholder="Open / Advanced",
        style=discord.TextStyle.short,
        required=True,
        max_length=50,
    )
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Pour quelle raison ?",
        placeholder="Triche, toxicité, sabotage, insultes, AFK, etc.",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    details: discord.ui.TextInput = discord.ui.TextInput(
        label="Détails / contexte",
        placeholder="Décris la situation : quand, où, ce qu'il s'est passé...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )
    evidence: discord.ui.TextInput = discord.ui.TextInput(
        label="Preuves (liens, clips, captures)",
        placeholder="Colle ici les liens vers tes preuves (facultatif)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_channel = await _open_ticket_channel(interaction, self.db)
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎫 Nouveau signalement - {ticket_channel.name}",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Joueur signalé", value=self.target.value, inline=False)
        embed.add_field(name="File concernée", value=self.queue.value, inline=False)
        embed.add_field(name="Raison", value=self.reason.value, inline=False)
        embed.add_field(name="Détails", value=self.details.value, inline=False)
        if self.evidence.value.strip():
            embed.add_field(name="Preuves", value=self.evidence.value, inline=False)
        embed.set_footer(text="Signalement anonyme")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] sending the initial message raised")
            await interaction.followup.send(
                "❌ Une erreur est survenue lors de l'envoi de ton signalement.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Ton signalement anonyme a bien été envoyé ({ticket_channel.mention}).",
            ephemeral=True,
        )


class RankModal(discord.ui.Modal, title="Candidature de file"):
    """Opens a rank application ticket (identified candidate).

    Asks 3 questions automatically and then creates a `ticket-{N}` channel
    in the `Tickets` category with a summary embed + close button.
    """

    rank: discord.ui.TextInput = discord.ui.TextInput(
        label="Pour quelle file postules-tu ?",
        placeholder="Advanced Queue",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Ton lien tracker",
        placeholder="https://tracker.gg/valorant/profile/...",
        style=discord.TextStyle.short,
        required=True,
        max_length=300,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Ton expérience tournois/LAN et/ou VLR",
        placeholder="Décris ton parcours compétitif : tournois, LAN, équipes VLR...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # The Modal is only attached to interactions sent inside a guild,
        # so `interaction.user` is a Member, not a User. Narrow explicitly
        # for the typing layer and as a runtime safety net.
        applicant = interaction.user
        if not isinstance(applicant, discord.Member):
            await interaction.followup.send(
                "❌ Cette action doit être utilisée dans un serveur.",
                ephemeral=True,
            )
            return
        ticket_channel = await _open_ticket_channel(interaction, self.db, member_access=applicant)
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎖️ Candidature de file - {ticket_channel.name}",
            color=0x9B59B6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
        embed.add_field(name="File visée", value=self.rank.value, inline=False)
        embed.add_field(name="Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="Expérience (tournois / LAN / VLR)",
            value=self.experience.value,
            inline=False,
        )
        embed.set_footer(text=f"Candidature de {interaction.user}")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] sending the initial message (rank) raised")
            await interaction.followup.send(
                "❌ Une erreur est survenue lors de l'envoi de ta candidature.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Ta candidature de file a bien été envoyée ({ticket_channel.mention}).",
            ephemeral=True,
        )


# ── Views ────────────────────────────────────────────────────────
class ApplicationReviewView(discord.ui.View):
    """Persistent view: rebuilds itself from the message's embed."""

    def __init__(self, db) -> None:
        super().__init__(timeout=None)
        self.db = db

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="application_accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        member, pseudo, is_staff, queue_tier = await self._validate_accept(interaction)
        if member is None:
            return
        if not await self._claim_accept(interaction):
            return

        await self._update_accept_embed(interaction, member, pseudo)
        await self._assign_accepted_roles(
            interaction, member, is_staff=is_staff, queue_tier=queue_tier
        )
        await self._notify_accepted_member(member, pseudo)
        await interaction.followup.send("✅ Candidature acceptée !", ephemeral=True)

    async def _validate_accept(
        self,
        interaction: discord.Interaction,
    ) -> tuple[discord.Member | None, str, bool, str | None]:
        """Parse the embed and resolve the applicant.

        Returns ``(member, pseudo, is_staff, queue_tier)``. ``member`` is
        ``None`` when the embed is corrupted or the applicant has left the
        guild — in both cases an error followup has already been sent.
        """
        applicant_id, pseudo, is_staff, queue_tier = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.followup.send(
                "❌ Données de candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return None, "", False, None
        member = interaction.guild.get_member(applicant_id)
        if not member:
            await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
            return None, "", False, None
        return member, pseudo, is_staff, queue_tier

    async def _claim_accept(self, interaction: discord.Interaction) -> bool:
        """Atomic CAS preventing double-handling. Returns False (and sends
        an error followup) if another admin already claimed this app."""
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="accepted",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ Cette candidature a déjà été traitée par un autre admin.",
                ephemeral=True,
            )
            return False
        return True

    async def _update_accept_embed(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        pseudo: str,
    ) -> None:
        """Rewrite the application embed to its accepted form. Best-effort."""
        try:
            old_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = discord.Embed(
                title="📋 Candidature acceptée",
                color=0x2ECC71,
                timestamp=datetime.now(UTC),
            )
            new_embed.set_thumbnail(url=member.display_avatar.url)
            new_embed.add_field(name="👤 Membre", value=member.mention, inline=True)
            new_embed.add_field(name="🎮 Pseudo", value=pseudo, inline=True)
            if old_embed:
                for field in old_embed.fields:
                    if field.name in (
                        QUEUE_TIER_FIELD_NAME,
                        "🔗 Tracker",
                        "🏆 Tournois / LAN",
                        "💼 Poste",
                        "📋 Expérience",
                        "Tracker",
                        "Tournois / LAN",
                        "Poste",
                        "Expérience",
                    ):
                        new_embed.add_field(name=field.name, value=field.value, inline=False)
            new_embed.add_field(
                name="✅ Acceptée par", value=interaction.user.mention, inline=False
            )
            await interaction.message.edit(embed=new_embed, view=None)
        except Exception:
            logger.exception("[accept] Edit failed")
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)

    async def _assign_accepted_roles(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        *,
        is_staff: bool,
        queue_tier: str | None,
    ) -> None:
        """Apply STAFF/PLAYERS + queue gating role. All best-effort."""
        roles = interaction.guild.roles
        await self._add_role_safe(
            member, discord.utils.get(roles, name=STAFF_ROLE if is_staff else PLAYERS_ROLE), "Role"
        )
        if is_staff:
            await self._add_role_safe(
                member, discord.utils.get(roles, name=PLAYERS_ROLE), "Members role"
            )
        if queue_tier and not is_staff:
            _, queue_role_name = QUEUE_TIERS[queue_tier]
            await self._add_named_role_or_warn(
                interaction, member, queue_role_name, "queue gating role"
            )

    async def _add_role_safe(
        self,
        member: discord.Member,
        role: discord.Role | None,
        label: str,
    ) -> None:
        """Add ``role`` if non-None; swallow errors."""
        if role is None:
            return
        try:
            await member.add_roles(role)
        except Exception:
            logger.exception("[accept] %s assignment failed", label)

    async def _add_named_role_or_warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role_name: str,
        label: str,
    ) -> None:
        """Like ``_add_role_safe`` but emits a warning if the role is missing
        on the guild (configuration error worth flagging in logs)."""
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                logger.exception("[accept] %s assignment failed", label)
            return
        logger.warning(
            "[accept] %s %r not found on guild %s; skipping",
            label,
            role_name,
            interaction.guild_id,
        )

    async def _notify_accepted_member(self, member: discord.Member, pseudo: str) -> None:
        """Rename to their declared pseudo + DM them an acceptance card."""
        with contextlib.suppress(Exception):
            await member.edit(nick=pseudo)
        with contextlib.suppress(discord.Forbidden):
            await member.send(
                embed=discord.Embed(
                    title="🎉 Candidature acceptée !",
                    description="Félicitations, tu as été accepté(e), tu peux désormais jouer aux 10mans !",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.danger,
        custom_id="application_refuse",
    )
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        applicant_id, _pseudo, _is_staff, _queue_tier = _parse_application_embed(
            interaction.message
        )
        if applicant_id is None:
            await interaction.response.send_message(
                "❌ Données de candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RefuseReasonModal(db=self.db, applicant_id=applicant_id)
        )


def _candidature_cooldown_remaining(db, user_id: str) -> float:
    """Returns seconds remaining on the candidature cooldown for user_id,
    or 0.0 if the user can apply now. Non-atomic peek — the atomic claim
    happens in ApplicationModal/StaffModal.on_submit so that abandoning
    the modal does not consume the cooldown."""
    doc = db["candidature_cooldowns"].find_one({"_id": user_id})
    if not doc:
        return 0.0
    last = doc["last_apply"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    diff = datetime.now(UTC) - last
    elapsed = diff.total_seconds()
    if elapsed >= CANDIDATURE_COOLDOWN_SECONDS:
        return 0.0
    return CANDIDATURE_COOLDOWN_SECONDS - elapsed


def _cooldown_message(remaining: float) -> str:
    minutes = int(remaining // 60)
    seconds = int(remaining % 60)
    return f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**."


class WelcomeView(discord.ui.View):
    """Persistent view in #verify: an Apply button for the gated Advanced
    queue, an instant-access Open queue button, and a
    Coach/Analyst/Manager button. The Advanced button opens an
    ApplicationModal tagged with its queue so the gating role can be
    auto-assigned on accept."""

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.review_view = review_view

    async def _send_application_modal(
        self, interaction: discord.Interaction, queue_tier: str
    ) -> None:
        remaining = _candidature_cooldown_remaining(self.db, str(interaction.user.id))
        if remaining > 0:
            await interaction.response.send_message(_cooldown_message(remaining), ephemeral=True)
            return
        await interaction.response.send_modal(
            ApplicationModal(db=self.db, review_view=self.review_view, queue_tier=queue_tier)
        )

    @discord.ui.button(
        label="Postuler File Advanced",
        style=discord.ButtonStyle.primary,
        custom_id="welcome_apply_advanced",
        row=0,
    )
    async def apply_advanced(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_application_modal(interaction, "advanced")

    @discord.ui.button(
        label="Rejoindre la File Open",
        style=discord.ButtonStyle.success,
        custom_id="welcome_apply_open",
        row=0,
    )
    async def apply_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        """La file Open est libre d'accès (aucun rôle requis) : ce bouton
        confirme simplement à l'utilisateur qu'il peut rejoindre la file
        Open, sans attribuer de rôle de file."""
        await interaction.response.send_message(
            "✅ La **file Open** est ouverte à tous : rends-toi sur "
            "`#open-queue` pour lancer une recherche de partie.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Coach / Analyste / Manager",
        style=discord.ButtonStyle.secondary,
        custom_id="welcome_apply_staff",
        row=1,
    )
    async def apply_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        remaining = _candidature_cooldown_remaining(self.db, str(interaction.user.id))
        if remaining > 0:
            await interaction.response.send_message(_cooldown_message(remaining), ephemeral=True)
            return
        await interaction.response.send_modal(StaffModal(db=self.db, review_view=self.review_view))


class CloseTicketView(discord.ui.View):
    """Persistent view: a 'Close ticket' button that deletes the channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Impossible de fermer ce salon ici.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(
                "🔒 Fermeture du ticket...",
                ephemeral=True,
            )
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "❌ Permission manquante pour supprimer ce salon.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            logger.exception("[ticket] deleting the channel raised")


class ReportView(discord.ui.View):
    """Persistent view: a 'Report' button that opens the ReportModal."""

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Signalement",
        style=discord.ButtonStyle.danger,
        custom_id="report_open_btn",
    )
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))


class TicketPanelView(discord.ui.View):
    """Persistent view: ticket opening panel with 2 options.

    - **Reports** -> ReportModal (anonymous report).
    - **Ranks**   -> RankModal (rank application, identified candidate).
    """

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Signalements",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_panel_reports_btn",
    )
    async def open_reports(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))

    @discord.ui.button(
        label="Candidature de file",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_panel_ranks_btn",
    )
    async def open_ranks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RankModal(db=self.db, close_view=self.close_view))


# ── Cog ──────────────────────────────────────────────────────────
class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db
        # Persistent view instances (registered via bot.add_view in setup).
        self.close_view = CloseTicketView()
        self.review_view = ApplicationReviewView(db=db)
        self.welcome_view = WelcomeView(db=db, review_view=self.review_view)
        self.report_view = ReportView(db=db, close_view=self.close_view)
        self.ticket_panel_view = TicketPanelView(db=db, close_view=self.close_view)

    @app_commands.command(
        name="welcome", description="Envoie le message de bienvenue dans le salon verify"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome(self, interaction: discord.Interaction) -> None:
        channel = discord.utils.get(interaction.guild.text_channels, name=WELCOME_CHANNEL)
        if not channel:
            await interaction.response.send_message("Salon verify introuvable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Bienvenue sur le Matchmaking de The Hub",
            description=(
                "Bienvenue sur un serveur **10mans** avec 2 files :\n\n"
                "• **File Open** - Ouverte à tous\n"
                "• **File Advanced** - Sur candidature\n\n"
                "Clique sur le bouton correspondant à la file que tu veux rejoindre. "
                "La **file Open** est libre d'accès ; la **file Advanced** "
                "passe par une rapide validation du staff.\n\n"
                "**Amuse-toi bien ! 🍀**"
            ),
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name)
        await channel.send(embed=embed, view=self.welcome_view)
        await interaction.response.send_message(
            f"Message envoyé dans {channel.mention} !", ephemeral=True
        )

    @welcome.error
    async def _welcome_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True
            )

    @app_commands.command(
        name="report",
        description="Affiche le panneau d'ouverture de ticket (Signalements / Candidatures) ici",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def report(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Cette commande doit être utilisée dans un salon textuel.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🎫 Ouvrir un ticket",
            description=(
                "Choisis le type de ticket que tu souhaites ouvrir :\n\n"
                "**Signalements** - Signale un joueur (triche, toxicité, "
                "sabotage, insultes, AFK...). Ton signalement est anonyme : "
                "ton identité n'est pas révélée au staff.\n\n"
                "**Candidature de file** - Postule pour une file privée. Nous "
                "te demanderons quelle file tu vises, les critères sont :\n"
                "• File Advanced : sur candidature\n"
                "• File Open : ouverte à tous"
            ),
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name if interaction.guild else "Tickets")
        await channel.send(embed=embed, view=self.ticket_panel_view)
        await interaction.response.send_message(
            f"Message envoyé dans {channel.mention} !",
            ephemeral=True,
        )

    @report.error
    async def _report_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "🚫 Réservé aux administrateurs.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = ApplicationsCog(bot, db)
    await bot.add_cog(cog)
    # Register persistent views (after restart, their custom_ids must be
    # routable by the bot even without a message instance).
    bot.add_view(cog.review_view)
    bot.add_view(cog.welcome_view)
    bot.add_view(cog.close_view)
    bot.add_view(cog.ticket_panel_view)
    # Kept to route the old "Report" panels already posted (custom_id
    # report_open_btn) after restart; new panels use ticket_panel_view.
    bot.add_view(cog.report_view)
