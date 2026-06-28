"""Lifecycle of dynamic match categories (create, delete, cleanup).

This module owns *all* operations on the per-match Discord category that
holds the match-preparation text channel and team voice channels. It
replaces the legacy 5-slot static system that relied on permanent
``Match #1``..``Match #5`` categories and roles.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import discord

logger = logging.getLogger(__name__)

MATCH_CATEGORY_PATTERN = re.compile(r"^Match #\d+$")


@dataclass(frozen=True)
class MatchChannels:
    """Concrete handles for the 4 Discord channels backing a match."""

    category: discord.CategoryChannel
    prep_channel: discord.TextChannel
    team1_vc: discord.VoiceChannel
    team2_vc: discord.VoiceChannel
    waiting_match_vc: discord.VoiceChannel


async def create_match_category(
    *,
    guild: discord.Guild,
    match_number: int,
    player_ids: Iterable[int],
    admin_role_ids: Iterable[int],
    viewer_role_ids: Iterable[int] = (),
    spectator_role_ids: Iterable[int] = (),
    hub_spectator_role_ids: Iterable[int] = (),
    team_prefix: str = "",
) -> MatchChannels:
    """Create a 'Match #N' category with 4 channels and proper overwrites.

    Overwrites are posted *on the category only*; child channels inherit
    via Discord's sync mechanism.

    - `viewer_role_ids` receive the same overwrites as players
      (view/send/connect/speak, without `manage_channels`).
    - `spectator_role_ids` can see the category and read history, but
      cannot join the voice channels nor send messages.
    - `hub_spectator_role_ids` see the category + voice channels (no
      connect, no send, no history) but the text prep channel is
      explicitly hidden via a per-channel `view_channel=False` override.
    """
    hub_spectator_role_ids = list(hub_spectator_role_ids)
    overwrites = _build_overwrites(
        guild=guild,
        player_ids=list(player_ids),
        admin_role_ids=list(admin_role_ids),
        viewer_role_ids=list(viewer_role_ids),
        spectator_role_ids=list(spectator_role_ids),
        hub_spectator_role_ids=hub_spectator_role_ids,
    )
    reason = f"Match #{match_number} created"
    prefix = f"{team_prefix} - " if team_prefix else ""
    category = await guild.create_category(
        f"Match #{match_number}", overwrites=overwrites, reason=reason
    )
    created: list = []
    try:
        prep = await category.create_text_channel(f"{prefix}match-preparation", reason=reason)
        created.append(prep)
        await _deny_prep_view_for_hub_spectators(
            prep_channel=prep,
            guild=guild,
            hub_spectator_role_ids=hub_spectator_role_ids,
            reason=reason,
        )
        team1 = await category.create_voice_channel(f"{prefix}Team 1", reason=reason)
        created.append(team1)
        team2 = await category.create_voice_channel(f"{prefix}Team 2", reason=reason)
        created.append(team2)
        waiting = await category.create_voice_channel(f"{prefix}Waiting Match", reason=reason)
        created.append(waiting)
    except Exception:
        logger.exception(
            "[match_category] partial creation failed for Match #%d, rolling back",
            match_number,
        )
        for ch in created:
            try:
                await ch.delete(reason="rollback partial match category creation")
            except Exception:
                logger.exception("[match_category] rollback delete child failed")
        try:
            await category.delete(reason="rollback partial match category creation")
        except Exception:
            logger.exception("[match_category] rollback delete category failed")
        raise
    return MatchChannels(
        category=category,
        prep_channel=prep,
        team1_vc=team1,
        team2_vc=team2,
        waiting_match_vc=waiting,
    )


_EVERYONE_OW = discord.PermissionOverwrite(view_channel=False, connect=False)
_PRIVILEGED_OW = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    connect=True,
    speak=True,
    manage_channels=True,
)
_PLAYER_OW = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    connect=True,
    speak=True,
)
_SPECTATOR_OW = discord.PermissionOverwrite(
    view_channel=True,
    read_message_history=True,
    send_messages=False,
    connect=False,
    speak=False,
)
_HUB_SPECTATOR_OW = discord.PermissionOverwrite(
    view_channel=True,
    read_message_history=False,
    send_messages=False,
    connect=False,
    speak=False,
)


def _apply_role_overwrites(
    overwrites: dict,
    guild: discord.Guild,
    role_ids: list[int] | None,
    ow: discord.PermissionOverwrite,
    *,
    override_existing: bool,
) -> None:
    """Resolve each role id and apply ``ow``.

    Roles that are not found on the guild are silently skipped (admins can
    add/remove roles between match formations). ``override_existing=False``
    skips roles that already have an overwrite from a higher-priority layer
    (e.g. an admin role also listed as a viewer keeps the privileged ow).
    """
    for role_id in role_ids or ():
        role = guild.get_role(role_id)
        if role is None:
            continue
        if not override_existing and role in overwrites:
            continue
        overwrites[role] = ow


def _build_overwrites(
    *,
    guild: discord.Guild,
    player_ids: list[int],
    admin_role_ids: list[int],
    viewer_role_ids: list[int] | None = None,
    spectator_role_ids: list[int] | None = None,
    hub_spectator_role_ids: list[int] | None = None,
) -> dict:
    """Build the permission overwrite matrix for a match category.

    Layers (applied highest-priority first; later layers don't override):

    - ``@everyone``: deny view + connect (private category).
    - Bot's top role: full privileged access + ``manage_channels``.
    - Admin roles: full privileged access + ``manage_channels``.
    - Viewer roles: view, send, connect, speak (no ``manage_channels``).
    - Spectator roles: view + read history, no send / connect / speak.
    - Hub-spectator roles: view only (no history / send / connect / speak).
      The prep text channel additionally posts an explicit view-deny
      override (see ``_deny_prep_view_for_hub_spectators``).
    - Players (by member ID): view, send, connect, speak.

    Members and roles missing from the guild are silently skipped.
    """
    overwrites: dict = {
        guild.default_role: _EVERYONE_OW,
        guild.me.top_role: _PRIVILEGED_OW,
    }
    _apply_role_overwrites(
        overwrites, guild, admin_role_ids, _PRIVILEGED_OW, override_existing=True
    )
    _apply_role_overwrites(overwrites, guild, viewer_role_ids, _PLAYER_OW, override_existing=False)
    _apply_role_overwrites(
        overwrites, guild, spectator_role_ids, _SPECTATOR_OW, override_existing=False
    )
    _apply_role_overwrites(
        overwrites, guild, hub_spectator_role_ids, _HUB_SPECTATOR_OW, override_existing=False
    )
    for uid in player_ids:
        member = guild.get_member(uid)
        if member is not None:
            overwrites[member] = _PLAYER_OW
    return overwrites


async def _deny_prep_view_for_hub_spectators(
    *,
    prep_channel: discord.TextChannel,
    guild: discord.Guild,
    hub_spectator_role_ids: list[int],
    reason: str,
) -> None:
    """Post a per-channel `view_channel=False` override on the prep text
    channel for each hub-spectator role so they cannot read it (Discord
    forbids "see name but not contents" — hiding is the only way).
    Voice channels stay visible via the inherited category overwrite.
    """
    for role_id in hub_spectator_role_ids:
        role = guild.get_role(role_id)
        if role is None:
            continue
        try:
            await prep_channel.set_permissions(
                role,
                view_channel=False,
                read_message_history=False,
                send_messages=False,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "[match_category] failed to deny prep view for role %s",
                role_id,
            )


async def delete_match_category(
    *,
    guild: discord.Guild,
    category_id: int,
    reason: str,
) -> None:
    """Delete the category and all its children. No-op if already gone."""
    channel = guild.get_channel(category_id)
    if channel is None:
        return
    if not isinstance(channel, discord.CategoryChannel):
        logger.warning(
            "[match_category] delete: id=%d is not a CategoryChannel (got %s)",
            category_id,
            type(channel).__name__,
        )
        return
    for child in list(channel.channels):
        try:
            await child.delete(reason=reason)
        except discord.NotFound:
            pass
        except Exception:
            logger.exception("[match_category] failed to delete child %s", child)
    try:
        await channel.delete(reason=reason)
    except discord.NotFound:
        pass
    except Exception:
        logger.exception("[match_category] failed to delete category %d", category_id)


async def cleanup_orphan_match_categories(
    *,
    guild: discord.Guild,
    active_category_ids: set[int],
) -> int:
    """Delete all 'Match #N' categories not referenced by an active match.

    Returns the number of categories that reached delete_match_category
    without an outer error (delete_match_category itself swallows internal
    errors).
    """
    deleted = 0
    for category in list(guild.categories):
        if not MATCH_CATEGORY_PATTERN.match(category.name):
            continue
        if category.id in active_category_ids:
            continue
        try:
            await delete_match_category(
                guild=guild,
                category_id=category.id,
                reason="Orphan match category cleanup",
            )
            deleted += 1
        except Exception:
            logger.exception(
                "[match_category] cleanup_orphan failed for %s (id=%d)",
                category.name,
                category.id,
            )
    return deleted
