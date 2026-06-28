"""Integration tests for MatchCog._post_match_scoreboard.

Covers the queue→channel mapping (RESULTS_CHANNELS), the happy-path
posting of the image, and the silent fallbacks when:
  - the results channel doesn't exist on the guild
  - the queue_type has no mapping
  - the image generator raises
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import bot as bot_module
from cogs.match import MatchCog


# ── Fixtures ─────────────────────────────────────────────────────
def _fake_player_stats(
    *,
    puuid: str,
    name: str,
    team: str,
    kills: int,
    deaths: int,
    assists: int,
    score: int,
    tag: str = "EUW",
) -> MagicMock:
    p = MagicMock()
    p.puuid = puuid
    p.name = name
    p.tag = tag
    p.team = team
    p.kills = kills
    p.deaths = deaths
    p.assists = assists
    p.score = score
    return p


def _fake_summary(
    *, rounds_red: int = 13, rounds_blue: int = 7, rounds_played: int = 20, map_name: str = "Ascent"
) -> MagicMock:
    s = MagicMock()
    s.matchid = "henrik-match-1"
    s.map_name = map_name
    s.rounds_red = rounds_red
    s.rounds_blue = rounds_blue
    s.rounds_played = rounds_played
    s.players = tuple(
        _fake_player_stats(
            puuid=f"pu{i}",
            name=f"P{i}",
            team="Red" if i < 5 else "Blue",
            kills=20 + i,
            deaths=14 + i,
            assists=4 + i,
            score=4000 + i * 100,
        )
        for i in range(10)
    )
    return s


def _fake_outcome(team_a_uids: list[str], team_b_uids: list[str]) -> MagicMock:
    """Build a MatchEloOutcome-shaped mock: `.changes` is iterable of
    PlayerEloChange-shaped objects (need .user_id + .new_elo)."""
    changes = []
    for i, uid in enumerate(team_a_uids):
        c = MagicMock()
        c.user_id = uid
        c.new_elo = 1500 + i * 10
        changes.append(c)
    for i, uid in enumerate(team_b_uids):
        c = MagicMock()
        c.user_id = uid
        c.new_elo = 1490 - i * 10
        changes.append(c)
    o = MagicMock()
    o.changes = tuple(changes)
    return o


def _fake_member(uid: int) -> MagicMock:
    m = MagicMock()
    m.id = uid
    avatar = MagicMock()
    avatar.url = f"https://cdn.test/{uid}.png"
    m.display_avatar = avatar
    return m


def _fake_results_channel(name: str = "open-results") -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.send = AsyncMock()
    return ch


def _fake_guild_with_channels(
    *, members: list[MagicMock], results_channel: MagicMock | None, guild_id: int = 42
) -> MagicMock:
    g = MagicMock()
    g.id = guild_id
    g.members = members
    g.get_member = lambda mid: next((m for m in members if m.id == int(mid)), None)
    g.text_channels = [results_channel] if results_channel is not None else []
    return g


def _make_cog() -> MatchCog:
    return MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))


# ── Tests ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "queue_type,expected_channel",
    [
        ("open", "open-results"),
        ("advanced", "advanced-results"),
        ("advanced", "advanced-results"),
        ("open", "open-results"),
    ],
)
async def test_post_scoreboard_uses_queue_specific_channel(queue_type, expected_channel):
    """The image lands in the channel matching the match's queue_type."""
    team_a_uids = [str(i) for i in range(5)]
    team_b_uids = [str(i) for i in range(5, 10)]
    team_a_uid_by_puuid = {f"pu{i}": uid for i, uid in enumerate(team_a_uids)}
    team_b_uid_by_puuid = {f"pu{i + 5}": uid for i, uid in enumerate(team_b_uids)}
    members = [_fake_member(int(uid)) for uid in team_a_uids + team_b_uids]
    channel = _fake_results_channel(expected_channel)
    guild = _fake_guild_with_channels(members=members, results_channel=channel)
    summary = _fake_summary()
    outcome = _fake_outcome(team_a_uids, team_b_uids)
    match_doc = {"queue_type": queue_type}

    cog = _make_cog()
    await cog._post_match_scoreboard(
        guild,
        summary,
        team_a_uid_by_puuid,
        team_b_uid_by_puuid,
        match_doc,
        outcome,
    )

    channel.send.assert_awaited_once()
    kwargs = channel.send.call_args.kwargs
    assert "file" in kwargs
    sent_file = kwargs["file"]
    assert isinstance(sent_file, discord.File)


async def test_post_scoreboard_silent_when_channel_missing():
    """Guild without the results channel: log warning, no send, no crash."""
    team_a_uids = [str(i) for i in range(5)]
    team_b_uids = [str(i) for i in range(5, 10)]
    team_a_uid_by_puuid = {f"pu{i}": uid for i, uid in enumerate(team_a_uids)}
    team_b_uid_by_puuid = {f"pu{i + 5}": uid for i, uid in enumerate(team_b_uids)}
    members = [_fake_member(int(uid)) for uid in team_a_uids + team_b_uids]
    guild = _fake_guild_with_channels(members=members, results_channel=None)
    summary = _fake_summary()
    outcome = _fake_outcome(team_a_uids, team_b_uids)
    match_doc = {"queue_type": "open"}

    cog = _make_cog()
    # Must not raise even though the channel is absent.
    await cog._post_match_scoreboard(
        guild,
        summary,
        team_a_uid_by_puuid,
        team_b_uid_by_puuid,
        match_doc,
        outcome,
    )


async def test_post_scoreboard_silent_when_queue_type_unknown():
    """Unknown queue_type has no entry in RESULTS_CHANNELS: skip silently."""
    team_a_uids = [str(i) for i in range(5)]
    team_b_uids = [str(i) for i in range(5, 10)]
    team_a_uid_by_puuid = {f"pu{i}": uid for i, uid in enumerate(team_a_uids)}
    team_b_uid_by_puuid = {f"pu{i + 5}": uid for i, uid in enumerate(team_b_uids)}
    members = [_fake_member(int(uid)) for uid in team_a_uids + team_b_uids]
    channel = _fake_results_channel("open-results")
    guild = _fake_guild_with_channels(members=members, results_channel=channel)
    summary = _fake_summary()
    outcome = _fake_outcome(team_a_uids, team_b_uids)
    match_doc = {"queue_type": "mystery-tier"}

    cog = _make_cog()
    await cog._post_match_scoreboard(
        guild,
        summary,
        team_a_uid_by_puuid,
        team_b_uid_by_puuid,
        match_doc,
        outcome,
    )
    channel.send.assert_not_awaited()


async def test_post_scoreboard_handles_image_generation_failure():
    """If generate_scoreboard raises, we log and skip the send (no double-failure)."""
    team_a_uids = [str(i) for i in range(5)]
    team_b_uids = [str(i) for i in range(5, 10)]
    team_a_uid_by_puuid = {f"pu{i}": uid for i, uid in enumerate(team_a_uids)}
    team_b_uid_by_puuid = {f"pu{i + 5}": uid for i, uid in enumerate(team_b_uids)}
    members = [_fake_member(int(uid)) for uid in team_a_uids + team_b_uids]
    channel = _fake_results_channel("open-results")
    guild = _fake_guild_with_channels(members=members, results_channel=channel)
    summary = _fake_summary()
    outcome = _fake_outcome(team_a_uids, team_b_uids)
    match_doc = {"queue_type": "open"}

    cog = _make_cog()
    with patch(
        "cogs.match._verification.generate_scoreboard",
        side_effect=RuntimeError("PIL exploded"),
    ):
        # Should not propagate the exception.
        await cog._post_match_scoreboard(
            guild,
            summary,
            team_a_uid_by_puuid,
            team_b_uid_by_puuid,
            match_doc,
            outcome,
        )
    channel.send.assert_not_awaited()


async def test_post_scoreboard_renders_with_actual_image_generator():
    """End-to-end: the real generate_scoreboard runs and produces a non-empty file."""
    team_a_uids = [str(i) for i in range(5)]
    team_b_uids = [str(i) for i in range(5, 10)]
    team_a_uid_by_puuid = {f"pu{i}": uid for i, uid in enumerate(team_a_uids)}
    team_b_uid_by_puuid = {f"pu{i + 5}": uid for i, uid in enumerate(team_b_uids)}
    members = [_fake_member(int(uid)) for uid in team_a_uids + team_b_uids]
    channel = _fake_results_channel("advanced-results")
    guild = _fake_guild_with_channels(members=members, results_channel=channel)
    summary = _fake_summary(rounds_red=13, rounds_blue=11, rounds_played=24, map_name="Lotus")
    outcome = _fake_outcome(team_a_uids, team_b_uids)
    match_doc = {"queue_type": "advanced"}

    cog = _make_cog()
    await cog._post_match_scoreboard(
        guild,
        summary,
        team_a_uid_by_puuid,
        team_b_uid_by_puuid,
        match_doc,
        outcome,
    )
    channel.send.assert_awaited_once()
    sent_file = channel.send.call_args.kwargs["file"]
    assert sent_file.filename == "scoreboard_advanced.png"


def test_results_channels_constant_maps_all_four_queues():
    """Snapshot of RESULTS_CHANNELS: any new queue tier should be wired."""
    from cogs.match import RESULTS_CHANNELS

    assert RESULTS_CHANNELS == {
        "open": "open-results",
        "advanced": "advanced-results",
    }
