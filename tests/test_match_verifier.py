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
from services.riot_api import (
    MatchPlayerStats,
    MatchSummary,
    RateLimitedError,
    RiotApiError,
)


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
def _client(history=(), *, by_puuid=None):
    """MagicMock HenrikDev client for the by-puuid history lookup.

    `by_puuid` maps a puuid to its history (or to an exception to raise)
    when a test needs a different answer per player."""
    client = MagicMock()
    if by_puuid is None:
        client.get_match_history_by_puuid.return_value = list(history)
    else:

        def _side_effect(region, puuid, **kwargs):
            res = by_puuid[puuid]
            if isinstance(res, Exception):
                raise res
            return res

        client.get_match_history_by_puuid.side_effect = _side_effect
    return client


def _ten_puuids() -> set[str]:
    return {f"p{i}" for i in range(10)}


def test_find_custom_returns_match_when_puuids_match():
    started = datetime.now(UTC)
    expected = {"a", "b", "c"}
    target = _summary(
        matchid="M_OK",
        started_at=started,
        players=tuple(_stats(p, "Red" if p in ("a", "b") else "Blue") for p in "abc"),
    )

    result = find_henrik_custom_match(
        _client([target]),
        region="eu",
        lookup_puuids=["a"],
        expected_puuids=expected,
        after=started - timedelta(minutes=5),
    )
    assert result is not None
    assert result.matchid == "M_OK"


def test_find_custom_queries_by_puuid_not_name_tag():
    """A player who renames their Riot ID makes the stored name#tag
    return HTTP 404 forever. The puuid is immutable, so the lookup must
    go through /v3/by-puuid/matches."""
    started = datetime.now(UTC)
    client = _client([])

    find_henrik_custom_match(
        client,
        region="eu",
        lookup_puuids=["puuid-1", "puuid-2"],
        expected_puuids={"puuid-1"},
        after=started,
    )

    client.get_match_history.assert_not_called()
    client.get_match_history_by_puuid.assert_called_once()
    args, kwargs = client.get_match_history_by_puuid.call_args
    assert args[0] == "eu"
    assert args[1] == "puuid-1"
    assert kwargs["mode"] == "custom"


def test_find_custom_skips_non_custom_mode():
    started = datetime.now(UTC)
    wrong_mode = _summary(
        matchid="M_COMP",
        mode="Competitive",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )

    result = find_henrik_custom_match(
        _client([wrong_mode]),
        region="eu",
        lookup_puuids=["a"],
        expected_puuids={"a", "b"},
        after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_skips_matches_before_after():
    too_old = _summary(
        matchid="M_OLD",
        started_at=datetime.now(UTC) - timedelta(hours=2),
        players=tuple(_stats(p, "Red") for p in "ab"),
    )

    result = find_henrik_custom_match(
        _client([too_old]),
        region="eu",
        lookup_puuids=["a"],
        expected_puuids={"a", "b"},
        after=datetime.now(UTC) - timedelta(minutes=30),
    )
    assert result is None


def test_find_custom_accepts_nine_of_ten_puuids():
    """A player joining the Valorant lobby on a second account leaves 9
    of the 10 registered puuids in the custom. Requiring all 10 silently
    dropped the whole match (no scoreboard, no Rating 2.0)."""
    started = datetime.now(UTC)
    # 9 registered puuids + 1 stranger => overlap 9/10
    lobby = [f"p{i}" for i in range(9)] + ["stranger"]
    partial = _summary(
        matchid="M_9_OF_10",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in lobby),
    )

    result = find_henrik_custom_match(
        _client([partial]),
        region="eu",
        lookup_puuids=["p0"],
        expected_puuids=_ten_puuids(),
        after=started - timedelta(minutes=5),
    )
    assert result is not None
    assert result.matchid == "M_9_OF_10"


def test_find_custom_rejects_eight_of_ten_puuids():
    """Below the tolerance the custom is probably another lobby."""
    started = datetime.now(UTC)
    lobby = [f"p{i}" for i in range(8)] + ["x", "y"]
    partial = _summary(
        matchid="M_8_OF_10",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in lobby),
    )

    result = find_henrik_custom_match(
        _client([partial]),
        region="eu",
        lookup_puuids=["p0"],
        expected_puuids=_ten_puuids(),
        after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_skips_when_puuids_incomplete():
    """With fewer than `min_overlap` registered puuids the whole set is
    required — a 3-player fixture cannot tolerate a miss."""
    started = datetime.now(UTC)
    partial = _summary(
        matchid="M_PARTIAL",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )

    result = find_henrik_custom_match(
        _client([partial]),
        region="eu",
        lookup_puuids=["a"],
        expected_puuids={"a", "b", "c"},
        after=started - timedelta(minutes=5),
    )
    assert result is None


def test_find_custom_returns_earliest_custom_after_cutoff():
    """The history comes back newest-first, but the match we verify is
    the FIRST custom started after the bot created it. Taking the newest
    attributed the next game's stats to the previous match whenever the
    group queued again before the verification ran."""
    started = datetime.now(UTC)
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

    result = find_henrik_custom_match(
        _client([newer, older]),
        region="eu",
        lookup_puuids=["a"],
        expected_puuids={"a", "b"},
        after=started - timedelta(hours=1),
    )
    assert result is not None
    assert result.matchid == "M_OLD"


def test_find_custom_returns_none_on_riot_error():
    client = _client(by_puuid={"a": RiotApiError("HenrikDev 503")})

    result = find_henrik_custom_match(
        client,
        region="eu",
        lookup_puuids=["a"],
        expected_puuids={"a"},
        after=datetime.now(UTC),
    )
    assert result is None


def test_find_custom_falls_back_to_next_puuid_on_api_error():
    """The leader's history can fail (private account, Henrik hiccup).
    Another player of the same lobby saw the same custom."""
    started = datetime.now(UTC)
    target = _summary(
        matchid="M_FALLBACK",
        started_at=started,
        players=tuple(_stats(p, "Red") for p in "ab"),
    )
    client = _client(by_puuid={"a": RiotApiError("boom"), "b": [target]})

    result = find_henrik_custom_match(
        client,
        region="eu",
        lookup_puuids=["a", "b"],
        expected_puuids={"a", "b"},
        after=started - timedelta(minutes=5),
    )
    assert result is not None
    assert result.matchid == "M_FALLBACK"


def test_find_custom_stops_immediately_on_rate_limit():
    """A 429 hits the whole key: retrying with another player only burns
    more quota."""
    client = _client(by_puuid={"a": RateLimitedError("429"), "b": []})

    result = find_henrik_custom_match(
        client,
        region="eu",
        lookup_puuids=["a", "b"],
        expected_puuids={"a", "b"},
        after=datetime.now(UTC),
    )
    assert result is None
    assert client.get_match_history_by_puuid.call_count == 1


def test_find_custom_returns_none_without_lookup_puuids():
    client = _client([])
    result = find_henrik_custom_match(
        client,
        region="eu",
        lookup_puuids=[],
        expected_puuids={"a"},
        after=datetime.now(UTC),
    )
    assert result is None
    client.get_match_history_by_puuid.assert_not_called()


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
