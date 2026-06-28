"""Tests for round-level + aggregate parsing in services/riot_api.py."""

from __future__ import annotations


def _henrik_min_match_with_damage():
    """Minimal Henrik /v3/matches response with damage + shots."""
    return {
        "data": {
            "metadata": {
                "matchid": "abc",
                "mode": "Custom Game",
                "map": "Ascent",
                "game_start": 1700000000,
                "rounds_played": 24,
            },
            "players": {
                "all_players": [
                    {
                        "puuid": "puuid-A",
                        "name": "Alice",
                        "tag": "EUW",
                        "team": "Red",
                        "character": "Jett",
                        "stats": {
                            "score": 5400,
                            "kills": 22,
                            "deaths": 14,
                            "assists": 5,
                            "headshots": 18,
                            "bodyshots": 50,
                            "legshots": 4,
                        },
                        "damage_made": 4123,
                        "damage_received": 3580,
                    },
                ],
            },
            "teams": {
                "red": {"rounds_won": 13},
                "blue": {"rounds_won": 11},
            },
            "rounds": [],
        }
    }


def test_parser_extracts_damage_and_shots():
    from services.riot_api import _parse_match

    summary = _parse_match(_henrik_min_match_with_damage()["data"])
    assert summary is not None
    p = summary.players[0]
    assert p.damage_made == 4123
    assert p.damage_received == 3580
    assert p.headshots == 18
    assert p.bodyshots == 50
    assert p.legshots == 4


# ---------------------------------------------------------------------------
# Round-event helpers
# ---------------------------------------------------------------------------


def _round(kill_events):
    """A minimal Henrik round dict with the given kill_events."""
    return {"kill_events": kill_events}


def _kill_event(*, killer_puuid, victim_puuid, kill_time_in_round, assistants=None):
    return {
        "killer_puuid": killer_puuid,
        "victim_puuid": victim_puuid,
        "kill_time_in_round": kill_time_in_round,
        "assistants": [{"assistant_puuid": pu} for pu in (assistants or [])],
    }


def _payload_with_rounds(rounds, players=None):
    return {
        "data": {
            "metadata": {
                "matchid": "abc",
                "mode": "Custom Game",
                "map": "Ascent",
                "game_start": 1700000000,
                "rounds_played": len(rounds),
            },
            "players": {"all_players": players or _two_player_roster()},
            "teams": {"red": {"rounds_won": 0}, "blue": {"rounds_won": 0}},
            "rounds": rounds,
        }
    }


def _two_player_roster():
    return [
        {
            "puuid": "A",
            "name": "A",
            "tag": "1",
            "team": "Red",
            "character": "Jett",
            "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
        },
        {
            "puuid": "B",
            "name": "B",
            "tag": "1",
            "team": "Blue",
            "character": "Sage",
            "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
        },
    ]


def test_parser_counts_first_kill_and_first_death():
    from services.riot_api import _parse_henrik_match

    rounds = [
        _round(
            [
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=12500),
                _kill_event(killer_puuid="B", victim_puuid="A", kill_time_in_round=14000),
            ]
        ),
    ]
    summary = _parse_henrik_match(_payload_with_rounds(rounds))
    pa = next(p for p in summary.players if p.puuid == "A")
    pb = next(p for p in summary.players if p.puuid == "B")
    assert pa.first_kills == 1
    assert pa.first_deaths == 0
    assert pb.first_kills == 0
    assert pb.first_deaths == 1


def test_parser_counts_multikills_per_round():
    from services.riot_api import _parse_henrik_match

    rounds = [
        _round(
            [
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=1000),
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=2000),
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=3000),
            ]
        ),
        _round(
            [
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=1000),
                _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=2000),
            ]
        ),
    ]
    summary = _parse_henrik_match(_payload_with_rounds(rounds))
    pa = next(p for p in summary.players if p.puuid == "A")
    assert pa.multikills_2k == 1
    assert pa.multikills_3k == 1
    assert pa.multikills_4k == 0
    assert pa.multikills_5k == 0


def test_parser_counts_kast_kill_or_assist_or_survive():
    from services.riot_api import _parse_henrik_match

    rounds = [
        _round([_kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=1000)]),
        _round([]),
    ]
    summary = _parse_henrik_match(_payload_with_rounds(rounds))
    pa = next(p for p in summary.players if p.puuid == "A")
    pb = next(p for p in summary.players if p.puuid == "B")
    assert pa.kast_rounds == 2  # round 1 (kill) + round 2 (survive)
    assert pb.kast_rounds == 1  # round 2 (survive) only


def test_parser_counts_traded_death_as_kast():
    from services.riot_api import _parse_henrik_match

    roster = [
        {
            "puuid": "A",
            "name": "A",
            "tag": "1",
            "team": "Red",
            "character": "X",
            "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
        },
        {
            "puuid": "B",
            "name": "B",
            "tag": "1",
            "team": "Blue",
            "character": "Y",
            "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
        },
        {
            "puuid": "C",
            "name": "C",
            "tag": "1",
            "team": "Red",
            "character": "Z",
            "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
        },
    ]
    rounds = [
        _round(
            [
                _kill_event(killer_puuid="B", victim_puuid="A", kill_time_in_round=10000),
                _kill_event(killer_puuid="C", victim_puuid="B", kill_time_in_round=13000),
            ]
        )
    ]
    summary = _parse_henrik_match(_payload_with_rounds(rounds, players=roster))
    pa = next(p for p in summary.players if p.puuid == "A")
    assert pa.kast_rounds == 1


def test_parser_kast_assist_credits_player():
    from services.riot_api import _parse_henrik_match

    rounds = [
        _round(
            [
                _kill_event(
                    killer_puuid="A", victim_puuid="B", kill_time_in_round=1000, assistants=["C"]
                ),
            ]
        )
    ]
    summary = _parse_henrik_match(
        _payload_with_rounds(
            rounds,
            players=[
                {
                    "puuid": "A",
                    "name": "A",
                    "tag": "1",
                    "team": "Red",
                    "character": "X",
                    "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
                },
                {
                    "puuid": "B",
                    "name": "B",
                    "tag": "1",
                    "team": "Blue",
                    "character": "Y",
                    "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
                },
                {
                    "puuid": "C",
                    "name": "C",
                    "tag": "1",
                    "team": "Red",
                    "character": "Z",
                    "stats": {"score": 0, "kills": 0, "deaths": 0, "assists": 0},
                },
            ],
        )
    )
    pc = next(p for p in summary.players if p.puuid == "C")
    assert pc.kast_rounds == 1


def test_parser_handles_missing_rounds_array():
    from services.riot_api import _parse_henrik_match

    payload = _payload_with_rounds([])
    payload["data"].pop("rounds")
    summary = _parse_henrik_match(payload)
    for p in summary.players:
        assert p.multikills_2k == 0
        assert p.first_kills == 0
        assert p.kast_rounds == 0


# ---------------------------------------------------------------------------
# Henrik production shape: kill_events nested under player_stats
# ---------------------------------------------------------------------------


def _round_production_shape(kill_events_by_killer):
    """A round whose kill_events live under ``player_stats[*].kill_events``.

    Henrik's real ``/v3/matches`` and ``/v2/match/{id}`` responses do NOT
    expose a round-level ``kill_events`` field — each player's
    ``player_stats`` entry only carries the kills *they* made. This helper
    builds that shape so the parser can be tested against production data.
    """
    return {
        "winning_team": "Red",
        "end_type": "Eliminated",
        "player_stats": [
            {"player_puuid": puuid, "kill_events": events}
            for puuid, events in kill_events_by_killer.items()
        ],
    }


def _payload_with_production_rounds(rounds, players=None):
    return {
        "data": {
            "metadata": {
                "matchid": "abc",
                "mode": "Custom Game",
                "map": "Ascent",
                "game_start": 1700000000,
                "rounds_played": len(rounds),
            },
            "players": {"all_players": players or _two_player_roster()},
            "teams": {"red": {"rounds_won": 0}, "blue": {"rounds_won": 0}},
            "rounds": rounds,
        }
    }


def test_parser_reads_kill_events_from_player_stats():
    """Regression: Henrik nests kill_events inside player_stats. Without
    this support, every player was credited "survived" every round, which
    pushed KAST to 100% across the board and inflated Rating 2.0 by ~0.2.
    """
    from services.riot_api import _parse_henrik_match

    rounds = [
        # Round 1: A kills B.
        _round_production_shape(
            {
                "A": [
                    _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=12500),
                ],
                "B": [],
            }
        ),
        # Round 2: nobody dies (both survive).
        _round_production_shape({"A": [], "B": []}),
    ]
    summary = _parse_henrik_match(_payload_with_production_rounds(rounds))
    pa = next(p for p in summary.players if p.puuid == "A")
    pb = next(p for p in summary.players if p.puuid == "B")
    # A: kill (R1) + survive (R2)  → 2 KAST.
    assert pa.kast_rounds == 2
    assert pa.first_kills == 1
    # B: died in R1 with no trade, survived R2 → 1 KAST (not 2).
    assert pb.kast_rounds == 1
    assert pb.first_deaths == 1


def test_parser_counts_multikills_from_player_stats_shape():
    from services.riot_api import _parse_henrik_match

    rounds = [
        _round_production_shape(
            {
                "A": [
                    _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=1000),
                    _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=2000),
                    _kill_event(killer_puuid="A", victim_puuid="B", kill_time_in_round=3000),
                ],
                "B": [],
            }
        ),
    ]
    summary = _parse_henrik_match(_payload_with_production_rounds(rounds))
    pa = next(p for p in summary.players if p.puuid == "A")
    assert pa.multikills_3k == 1
    assert pa.multikills_2k == 0
