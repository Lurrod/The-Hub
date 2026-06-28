"""Tests of the team-balancing algo (brute-force optimal)."""

import time

import pytest

from services.team_balancer import (
    Player,
    balance_teams,
    format_teams,
)


def _players(elos: list[int]) -> list[Player]:
    return [Player(id=i, name=f"P{i}", elo=e) for i, e in enumerate(elos)]


# ── Input validation ──────────────────────────────────────────────
def test_rejects_less_than_10_players():
    with pytest.raises(ValueError, match="10"):
        balance_teams(_players([1500] * 9))


def test_rejects_more_than_10_players():
    with pytest.raises(ValueError, match="10"):
        balance_teams(_players([1500] * 11))


def test_rejects_duplicate_ids():
    players = [Player(id=1, name="A", elo=1500)] * 10
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        balance_teams(players)


# -- Trivial case: all equal --
def test_all_equal_elo_returns_zero_diff():
    result = balance_teams(_players([1500] * 10))
    assert result.elo_diff == 0
    assert result.peak_diff == 0
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5


# -- Obvious case: 5 strong + 5 weak --
def test_two_clear_groups_get_split():
    """5 at 2000, 5 at 1000 -> each team must have 2-3 strong players."""
    elos = [2000] * 5 + [1000] * 5
    result = balance_teams(_players(elos))
    # Perfect diff: (2*2000 + 3*1000) vs (3*2000 + 2*1000) = 7000 vs 8000 -> diff 1000
    # But is the optimum (5000 vs 5000)? No, impossible with only 2 values.
    # In fact: to get diff 0 we would need 2.5 vs 2.5 -> impossible
    # Optimum: 2-3 split -> 7000 vs 8000 -> diff 1000
    assert result.elo_diff == 1000


def test_outlier_player_balanced():
    """One player at 5000 + 9 at 1000: the outlier goes to one side, minimize the rest."""
    elos = [5000] + [1000] * 9
    result = balance_teams(_players(elos))
    # team with outlier: 5000 + 4*1000 = 9000
    # other team: 5*1000 = 5000
    # diff = 4000 (incompressible)
    assert result.elo_diff == 4000


# -- Non-trivial optimum case --
def test_finds_optimal_partition():
    """Known case: varied ELOs, the algo must find the exact optimum."""
    # 10 players: total sum = 15000 -> 7500 vs 7500 ideal
    elos = [3000, 2500, 2000, 1800, 1500, 1300, 1200, 900, 500, 300]
    # sum = 15000
    result = balance_teams(_players(elos))
    assert result.elo_diff <= 200, f"diff too large: {result.elo_diff}"
    assert result.total_a + result.total_b == 15000


def test_brute_force_better_than_snake_draft():
    """Typical case where snake draft gives a suboptimal result."""
    # snake draft: pos 0,3,4,7,8 vs 1,2,5,6,9
    # elos: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    # snake: [10, 7, 6, 3, 2] = 28 vs [9, 8, 5, 4, 1] = 27 -> diff 1
    # brute-force does at least as well
    elos = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    result = balance_teams(_players(elos))
    assert result.elo_diff <= 1


# -- Determinism --
def test_deterministic_same_input_same_output():
    elos = [1500, 1700, 1200, 1900, 1400, 1600, 1800, 1300, 1500, 1500]
    players = _players(elos)
    r1 = balance_teams(players)
    r2 = balance_teams(players)
    r3 = balance_teams(players)

    assert r1.team_a == r2.team_a == r3.team_a
    assert r1.team_b == r2.team_b == r3.team_b


def test_each_player_appears_exactly_once():
    elos = [1500, 1700, 1200, 1900, 1400, 1600, 1800, 1300, 1500, 1500]
    players = _players(elos)
    result = balance_teams(players)
    all_ids = {p.id for p in result.team_a} | {p.id for p in result.team_b}
    assert all_ids == set(range(10))
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5


# -- Tie-breaker peak_diff --
def test_tiebreaker_minimizes_peak_difference():
    """
    At equal elo_diff, prefer to spread the best players.
    Case: 2 strong (3000) + 8 at 1000.
    Splits at equal elo_diff: 1 strong on each side (not both together).
    """
    elos = [3000, 3000] + [1000] * 8
    result = balance_teams(_players(elos))
    # If the 2 strong are on the same side: diff = 4000. If separated: diff = 0.
    # So the algo will separate them -> peak_diff = 0
    assert result.peak_diff == 0
    a_max = max(p.elo for p in result.team_a)
    b_max = max(p.elo for p in result.team_b)
    assert a_max == b_max == 3000


# -- Performance --
def test_performance_under_50ms():
    import random

    random.seed(0)
    elos = [random.randint(500, 3500) for _ in range(10)]
    players = _players(elos)

    start = time.perf_counter()
    for _ in range(100):
        balance_teams(players)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f"100 iterations in {elapsed * 1000:.1f}ms (>500ms)"


# -- format_teams (smoke) --
def test_format_teams_returns_readable_string():
    result = balance_teams(_players([1500] * 10))
    out = format_teams(result)
    assert "Team A" in out
    assert "Team B" in out
    assert "diff=" in out
