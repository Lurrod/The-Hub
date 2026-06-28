"""Tests for persisting per-match ELO deltas and round scores.

Covers:
  * services.match_verifier.compute_team_scores (pure mapping of Henrik
    Red/Blue scores onto team_a/team_b)
  * services.elo_updater.build_elo_results (MatchEloOutcome -> dict)
  * services.repository.set_match_elo_results / set_match_score (persist
    on the match doc)
"""

from __future__ import annotations

from types import SimpleNamespace

from services import repository
from services.elo_updater import (
    MatchEloOutcome,
    PlayerEloChange,
    build_elo_results,
)
from services.match_verifier import compute_round_breakdown, compute_team_scores


def _summary(red: int, blue: int, players: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(rounds_red=red, rounds_blue=blue, players=players)


def _p(puuid: str, team: str) -> SimpleNamespace:
    return SimpleNamespace(puuid=puuid, team=team)


# -- compute_team_scores -------------------------------------------------


def test_team_a_on_red_gets_red_score():
    summary = _summary(13, 9, [_p("a1", "Red"), _p("a2", "Red"), _p("b1", "Blue")])
    score_a, score_b = compute_team_scores(
        summary,
        {"a1": "1", "a2": "2"},
        {"b1": "3"},
    )
    assert (score_a, score_b) == (13, 9)


def test_team_a_on_blue_gets_blue_score():
    summary = _summary(13, 9, [_p("a1", "Blue"), _p("b1", "Red")])
    score_a, score_b = compute_team_scores(summary, {"a1": "1"}, {"b1": "2"})
    assert (score_a, score_b) == (9, 13)


def test_ambiguous_team_a_side_returns_none():
    # team_a's players are split across both Henrik sides -> cannot map.
    summary = _summary(13, 9, [_p("a1", "Red"), _p("a2", "Blue")])
    assert compute_team_scores(summary, {"a1": "1", "a2": "2"}, {}) == (None, None)


def test_no_matched_players_returns_none():
    summary = _summary(13, 9, [_p("x1", "Red")])
    assert compute_team_scores(summary, {"unknown": "1"}, {"x1": "2"}) == (None, None)


# -- build_elo_results ---------------------------------------------------


def test_build_elo_results_shape():
    outcome = MatchEloOutcome(
        avg_elo=2000,
        gain=20,
        loss=20,
        changes=(
            PlayerEloChange(user_id="1", name="A", old_elo=2000, new_elo=2022, delta=22, win=True),
            PlayerEloChange(
                user_id="2", name="B", old_elo=2100, new_elo=2082, delta=-18, win=False
            ),
        ),
    )
    results = build_elo_results(outcome)
    assert results == {
        "1": {"delta": 22, "old": 2000, "new": 2022, "win": True},
        "2": {"delta": -18, "old": 2100, "new": 2082, "win": False},
    }


# -- repository persistence ---------------------------------------------


def test_set_match_elo_results_persists_on_match_doc(mongo_db):
    col = repository.get_matches_col(mongo_db)
    col.insert_one({"_id": "m1", "status": "validated_a"})
    repository.set_match_elo_results(
        mongo_db,
        "m1",
        {"1": {"delta": 22, "old": 2000, "new": 2022, "win": True}},
    )
    doc = col.find_one({"_id": "m1"})
    assert doc["elo_results"]["1"]["delta"] == 22
    assert doc["elo_results"]["1"]["win"] is True


def test_set_match_score_persists_on_match_doc(mongo_db):
    col = repository.get_matches_col(mongo_db)
    col.insert_one({"_id": "m2", "status": "validated_b"})
    repository.set_match_score(mongo_db, "m2", 11, 13)
    doc = col.find_one({"_id": "m2"})
    assert doc["score_a"] == 11
    assert doc["score_b"] == 13


# -- compute_round_breakdown --------------------------------------------


def _round_summary(round_winners, round_end_types, players):
    return SimpleNamespace(
        round_winners=tuple(round_winners),
        round_end_types=tuple(round_end_types),
        players=players,
    )


def test_round_breakdown_maps_red_blue_to_team_a_b():
    # team_a on Red side: Red rounds -> "a", Blue rounds -> "b".
    summary = _round_summary(
        ["Red", "Blue", "Red"],
        ["Eliminated", "Bomb defused", "Bomb detonated"],
        [_p("a1", "Red"), _p("b1", "Blue")],
    )
    rounds = compute_round_breakdown(summary, {"a1": "1"}, {"b1": "2"})
    assert rounds == [
        {"winner": "a", "end": "Eliminated"},
        {"winner": "b", "end": "Bomb defused"},
        {"winner": "a", "end": "Bomb detonated"},
    ]


def test_round_breakdown_team_a_on_blue_inverts():
    summary = _round_summary(["Red", "Blue"], ["", ""], [_p("a1", "Blue"), _p("b1", "Red")])
    rounds = compute_round_breakdown(summary, {"a1": "1"}, {"b1": "2"})
    assert [r["winner"] for r in rounds] == ["b", "a"]


def test_round_breakdown_blank_when_side_ambiguous():
    summary = _round_summary(["Red", "Blue"], ["", ""], [_p("a1", "Red"), _p("a2", "Blue")])
    rounds = compute_round_breakdown(summary, {"a1": "1", "a2": "2"}, {})
    assert [r["winner"] for r in rounds] == ["", ""]


def test_set_match_rounds_persists_on_match_doc(mongo_db):
    col = repository.get_matches_col(mongo_db)
    col.insert_one({"_id": "m3", "status": "validated_a"})
    rounds = [{"winner": "a", "end": "Eliminated"}, {"winner": "b", "end": "Bomb defused"}]
    repository.set_match_rounds(mongo_db, "m3", rounds)
    doc = col.find_one({"_id": "m3"})
    assert doc["rounds"] == rounds
    assert len(doc["rounds"]) == 2


# -- acs_sum aggregation -------------------------------------------------


def test_acs_sum_accumulates_in_rating_aggregates(mongo_db):
    deltas = [
        {
            "user_id": "1",
            "queue_type": "open",
            "games": 1,
            "acs_sum": 250.0,
            "acs_games": 1,
            "rating_2_0_sum": 1.2,
        },
        {
            "user_id": "1",
            "queue_type": "open",
            "games": 1,
            "acs_sum": 200.0,
            "acs_games": 1,
            "rating_2_0_sum": 1.0,
        },
    ]
    repository.update_rating_aggregates(mongo_db, deltas)
    agg = repository.get_rating_aggregate(mongo_db, user_id="1", queue_type="open")
    assert agg["games"] == 2
    assert agg["acs_sum"] == 450.0
    assert agg["acs_games"] == 2


def test_acs_games_counts_only_deltas_carrying_acs(mongo_db):
    """Un delta sans acs_sum/acs_games (vieux flux) ne doit pas gonfler le
    denominateur de l'ACS saison."""
    deltas = [
        {"user_id": "2", "queue_type": "open", "games": 1, "rating_2_0_sum": 1.0},
        {
            "user_id": "2",
            "queue_type": "open",
            "games": 1,
            "acs_sum": 300.0,
            "acs_games": 1,
            "rating_2_0_sum": 1.1,
        },
    ]
    repository.update_rating_aggregates(mongo_db, deltas)
    agg = repository.get_rating_aggregate(mongo_db, user_id="2", queue_type="open")
    assert agg["games"] == 2
    assert agg["acs_sum"] == 300.0
    assert agg["acs_games"] == 1
