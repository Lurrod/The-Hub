"""Tests of the pure match formation logic."""

import random

import pytest

from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
)
from services.team_balancer import Player


def _riot_doc(name: str = "X") -> dict:
    return {
        "riot_name": name,
        "riot_tag": "EUW",
        "riot_region": "eu",
        "puuid": "p",
        "peak_elo": 2500,
        "source": "peak_recent",
    }


# ── build_players ─────────────────────────────────────────────────
def test_build_players_uses_member_display_name():
    players = build_players(
        player_ids=["1", "2"],
        riot_accounts={"1": _riot_doc(), "2": _riot_doc()},
        member_names={"1": "Jet", "2": "Sage"},
        bot_elos={"1": 1500, "2": 2000},
    )
    assert len(players) == 2
    assert players[0].id == 1 and players[0].name == "Jet" and players[0].elo == 1500
    assert players[1].id == 2 and players[1].name == "Sage" and players[1].elo == 2000


def test_build_players_skips_unlinked():
    players = build_players(
        player_ids=["1", "2", "3"],
        riot_accounts={"1": _riot_doc(), "3": _riot_doc()},
        member_names={"1": "A", "2": "B", "3": "C"},
        bot_elos={"1": 1500, "2": 999, "3": 1700},
    )
    # Player 2 without a Riot account -> ignored (even if they have a bot ELO)
    assert len(players) == 2
    assert {p.id for p in players} == {1, 3}


def test_build_players_falls_back_to_riot_name():
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc(name="RiotName")},
        member_names={},  # no resolved member
        bot_elos={"1": 1500},
    )
    assert players[0].name == "RiotName"


def test_build_players_uses_bot_elo_not_riot():
    """Matchmaking ELO comes from the shared `elo` collection, never from riot_accounts."""
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc()},  # peak_elo 2500 ignored
        member_names={"1": "A"},
        bot_elos={"1": 1234},  # source of truth
    )
    assert players[0].elo == 1234


def test_build_players_zero_when_no_bot_elo():
    """If the shared `elo` collection has no doc for the player, ELO = 0."""
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc()},
        member_names={"1": "A"},
        bot_elos={},
    )
    assert players[0].elo == 0


# ── plan_match ────────────────────────────────────────────────────
def test_plan_match_rejects_wrong_size():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(9)]
    with pytest.raises(ValueError, match="10"):
        plan_match(players, free_category="Match #1")


def test_plan_match_returns_balanced_teams_and_random_choices():
    players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    rng = random.Random(42)  # deterministic for the test

    plan = plan_match(players, free_category="Match #1", rng=rng)

    assert len(plan.teams.team_a) == 5
    assert len(plan.teams.team_b) == 5
    # with a seeded rng we can verify stability
    assert plan.map_name in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert plan.lobby_leader in players
    assert plan.category_name == "Match #1"


def test_plan_match_with_no_free_category():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(10)]
    plan = plan_match(players, free_category=None)
    assert plan.category_name is None


def test_plan_match_lobby_leader_is_one_of_the_players():
    players = [Player(id=i, name=f"P{i}", elo=1000 + i) for i in range(10)]
    for seed in range(20):
        plan = plan_match(players, free_category="Match #1", rng=random.Random(seed))
        leader_ids = {p.id for p in players}
        assert plan.lobby_leader.id in leader_ids


def test_plan_match_balance_optimal():
    """Known case: with 10 varied ELOs, the brute-force algo finds the best split."""
    players = [
        Player(id=i, name=f"P{i}", elo=elo)
        for i, elo in enumerate([3000, 2500, 2000, 1800, 1500, 1300, 1200, 900, 500, 300])
    ]
    plan = plan_match(players, free_category=None)
    assert plan.teams.elo_diff <= 200


# -- serialize_team --
def test_serialize_team_returns_list_of_dicts():
    team = (Player(id=1, name="A", elo=1500), Player(id=2, name="B", elo=1600))
    out = serialize_team(team)
    assert out == [
        {"id": 1, "name": "A", "elo": 1500},
        {"id": 2, "name": "B", "elo": 1600},
    ]


# ── build_plan_from_draft ─────────────────────────────────────────
from types import SimpleNamespace


def _p_draft(uid: int, elo: int = 2000) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def _draft_result(team_a_ids: list, team_b_ids: list):
    """Build a duck-typed DraftResult for build_plan_from_draft."""
    team_a = tuple(_p_draft(i) for i in team_a_ids)
    team_b = tuple(_p_draft(i) for i in team_b_ids)
    return SimpleNamespace(
        cap_a=team_a[0],
        cap_b=team_b[0],
        team_a=team_a,
        team_b=team_b,
    )


def test_build_plan_from_draft_uses_provided_map_name():
    from services.match_service import build_plan_from_draft

    result = _draft_result([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(0),
        map_name="Haven",
    )
    assert plan.map_name == "Haven"
    assert plan.category_name == "Match #1"
    assert plan.lobby_leader.id == 1  # cap_a
    assert plan.teams.team_a == result.team_a
    assert plan.teams.team_b == result.team_b


def test_build_plan_from_draft_falls_back_to_random_map_when_none():
    from services.elo_calc import MAPS
    from services.match_service import build_plan_from_draft

    result = _draft_result([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(0),
        map_name=None,
    )
    assert plan.map_name in MAPS


def test_build_plan_from_draft_computes_elo_diff_and_peak_diff():
    from services.match_service import build_plan_from_draft

    team_a = tuple(Player(id=i, name=f"A{i}", elo=3000) for i in range(1, 6))
    team_b = tuple(Player(id=i, name=f"B{i}", elo=2000) for i in range(6, 11))
    result = SimpleNamespace(cap_a=team_a[0], cap_b=team_b[0], team_a=team_a, team_b=team_b)
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(0),
        map_name="Ascent",
    )
    assert plan.teams.elo_diff == 5000
    assert plan.teams.peak_diff == 1000
