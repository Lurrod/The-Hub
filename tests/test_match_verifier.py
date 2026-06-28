"""
Tests for the services/match_verifier.py module.

Covers:
  - `find_henrik_custom_match`: lookup of a recent custom match
    containing the 10 expected puuids.
  - `compute_acs_multipliers`: per-player ACS multiplier computation,
    clamped to [0.7, 1.3], with handling of degenerate cases
    (mixed teams, tie, avg_acs=0).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from services.match_verifier import (
    DEFAULT_MULT_MAX,
    DEFAULT_MULT_MIN,
    compute_acs_multipliers,
    find_henrik_custom_match,
)
from services.riot_api import MatchPlayerStats, MatchSummary, RiotApiError


# ── Helpers ──────────────────────────────────────────────────────
def _stats(puuid: str, team: str, score: int = 100, name: str = "P") -> MatchPlayerStats:
    return MatchPlayerStats(
        puuid=puuid,
        name=name,
        tag="EUW",
        team=team,
        score=score,
        kills=0,
        deaths=0,
        assists=0,
    )


def _summary(
    *,
    matchid: str = "M1",
    mode: str = "Custom Game",
    started_at: datetime | None = None,
    rounds: int = 24,
    rounds_red: int = 13,
    rounds_blue: int = 11,
    players: tuple[MatchPlayerStats, ...] = (),
) -> MatchSummary:
    return MatchSummary(
        matchid=matchid,
        mode=mode,
        map_name="Ascent",
        started_at=started_at or datetime.now(UTC),
        rounds_played=rounds,
        players=players,
        rounds_red=rounds_red,
        rounds_blue=rounds_blue,
    )


# ── find_henrik_custom_match ──────────────────────────────────────
def test_find_custom_returns_match_when_puuids_match():
    started = datetime.now(UTC)
    expected = {"a", "b", "c"}
    target = _summary(
        matchid="M_OK",
        started_at=started,
        players=tuple(_stats(p, "Red" if p in ("a", "b") else "Blue") for p in "abc"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [target]

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids=expected,
        after=started - timedelta(minutes=5),
    )
    assert result is not None
    assert result.matchid == "M_OK"


def test_find_custom_skips_non_custom_mode():
    started = datetime.now(UTC)
    expected = {"a", "b"}
    # Mode "Competitive" but contains the right puuids
    wrong_mode = _summary(
        matchid="M_COMP",
        mode="Competitive",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [wrong_mode]

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids=expected,
        after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_skips_matches_before_after():
    expected = {"a", "b"}
    too_old = _summary(
        matchid="M_OLD",
        started_at=datetime.now(UTC) - timedelta(hours=2),
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [too_old]

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids=expected,
        after=datetime.now(UTC) - timedelta(minutes=30),
    )
    assert result is None


def test_find_custom_skips_when_puuids_incomplete():
    started = datetime.now(UTC)
    expected = {"a", "b", "c"}  # 3 expected
    # The match only has 2 of the 3 puuids
    partial = _summary(
        matchid="M_PARTIAL",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [partial]

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids=expected,
        after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_returns_none_on_riot_error():
    client = MagicMock()
    client.get_match_history.side_effect = RiotApiError("HenrikDev 503")

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids={"a"},
        after=datetime.now(UTC),
    )
    assert result is None


def test_find_custom_returns_first_matching_in_history():
    """The client returns the history from most recent to oldest. We must
    take the first one that matches, not the last."""
    started = datetime.now(UTC)
    expected = {"a", "b"}
    newer = _summary(
        matchid="M_NEW",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    older = _summary(
        matchid="M_OLD",
        started_at=started - timedelta(minutes=10),
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = MagicMock()
    client.get_match_history.return_value = [newer, older]

    result = find_henrik_custom_match(
        client,
        region="eu",
        leader_name="L",
        leader_tag="T",
        expected_puuids=expected,
        after=started - timedelta(hours=1),
    )
    assert result is not None
    assert result.matchid == "M_NEW"


# ── compute_acs_multipliers ───────────────────────────────────────
def test_acs_happy_path_team_a_wins():
    """Team A (Red) wins 13-11; all players have the same score = mult ~1.0."""
    players = (
        # 5 sur Red (Team A)
        _stats("a1", "Red", score=2400),
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Red", score=2400),
        _stats("a5", "Red", score=2400),
        # 5 sur Blue (Team B)
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == "Red"
    assert len(result.performances) == 10
    # All mults = 1.0 since acs equals avg_acs
    for p in result.performances:
        assert p.multiplier == pytest.approx(1.0, abs=0.01)
    # Team A (Red) wins
    team_a_perfs = [p for p in result.performances if p.user_id.startswith("uid_a")]
    assert all(p.win for p in team_a_perfs)
    team_b_perfs = [p for p in result.performances if p.user_id.startswith("uid_b")]
    assert not any(p.win for p in team_b_perfs)


def test_acs_top_frag_gets_higher_multiplier():
    """A player with double ACS must have a higher mult (clamped to 1.3)."""
    players = (
        _stats("a1", "Red", score=4800),  # top frag : 2x la moyenne
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Red", score=2400),
        _stats("a5", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    top = next(p for p in result.performances if p.user_id == "uid_a1")
    assert top.multiplier == DEFAULT_MULT_MAX  # clamped to 1.3


def test_acs_bottom_frag_clamped_to_min():
    """A player with near-zero ACS must be clamped to 0.7."""
    players = (
        _stats("a1", "Red", score=0),  # bottom frag
        _stats("a2", "Red", score=3000),
        _stats("a3", "Red", score=3000),
        _stats("a4", "Red", score=3000),
        _stats("a5", "Red", score=3000),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Blue", score=2400),
        _stats("b5", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    bottom = next(p for p in result.performances if p.user_id == "uid_a1")
    assert bottom.multiplier == DEFAULT_MULT_MIN  # clamped to 0.7


def test_acs_mixed_team_labels_skipped():
    """If the bot's Team A players are spread between Red and Blue on the
    Henrik side (lobby where players switched A/D), we skip that team."""
    players = (
        _stats("a1", "Red", score=2400),  # 3 Red
        _stats("a2", "Red", score=2400),
        _stats("a3", "Red", score=2400),
        _stats("a4", "Blue", score=2400),  # but 2 Blue!
        _stats("a5", "Blue", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
        _stats("b3", "Blue", score=2400),
        _stats("b4", "Red", score=2400),
        _stats("b5", "Red", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {f"a{i}": f"uid_a{i}" for i in range(1, 6)}
    team_b = {f"b{i}": f"uid_b{i}" for i in range(1, 6)}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    # No perf computed because both teams are mixed
    assert len(result.performances) == 0


def test_acs_handles_tie_with_empty_winning_team():
    """If both teams have the same round count, winning_team = ''."""
    players = (
        _stats("a1", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=12, rounds_blue=12, players=players)
    team_a = {"a1": "uid_a1"}
    team_b = {"b1": "uid_b1"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == ""
    # Nobody wins
    for p in result.performances:
        assert p.win is False


def test_acs_zero_avg_falls_back_to_one():
    """If the whole team has a score of 0 (avg=0), no division by zero."""
    players = (
        _stats("a1", "Red", score=0),
        _stats("a2", "Red", score=0),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=13, rounds_blue=11, players=players)
    team_a = {"a1": "uid_a1", "a2": "uid_a2"}
    team_b = {"b1": "uid_b1", "b2": "uid_b2"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    # Team A : avg_acs=0 → fallback 1.0, acs=0/1.0=0 → clamp 0.7
    team_a_perfs = [p for p in result.performances if p.user_id.startswith("uid_a")]
    assert len(team_a_perfs) == 2
    for p in team_a_perfs:
        assert p.multiplier == DEFAULT_MULT_MIN  # clamped to 0.7


def test_acs_team_b_wins_correctly_labeled():
    """When Blue wins, Blue players are marked win=True."""
    players = (
        _stats("a1", "Red", score=2400),
        _stats("a2", "Red", score=2400),
        _stats("b1", "Blue", score=2400),
        _stats("b2", "Blue", score=2400),
    )
    match = _summary(rounds=24, rounds_red=11, rounds_blue=13, players=players)
    team_a = {"a1": "uid_a1", "a2": "uid_a2"}
    team_b = {"b1": "uid_b1", "b2": "uid_b2"}

    result = compute_acs_multipliers(match, team_a_uid_by_puuid=team_a, team_b_uid_by_puuid=team_b)
    assert result.winning_team == "Blue"
    for p in result.performances:
        if p.user_id.startswith("uid_b"):
            assert p.win is True
        else:
            assert p.win is False
