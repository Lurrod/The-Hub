"""Tests for build_extended_stats in services/match_verifier.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _summary_with_two_players():
    from services.riot_api import MatchPlayerStats, MatchSummary

    return MatchSummary(
        matchid="m-1",
        mode="Custom Game",
        map_name="Ascent",
        started_at=datetime(2026, 6, 1, tzinfo=UTC),
        rounds_played=24,
        players=(
            MatchPlayerStats(
                puuid="P-A",
                name="A",
                tag="1",
                team="Red",
                score=5400,
                kills=22,
                deaths=14,
                assists=5,
                agent="Jett",
                damage_made=4123,
                damage_received=3580,
                headshots=18,
                bodyshots=50,
                legshots=4,
                multikills_2k=3,
                multikills_3k=1,
                multikills_4k=0,
                multikills_5k=0,
                first_kills=4,
                first_deaths=2,
                kast_rounds=19,
            ),
            MatchPlayerStats(
                puuid="P-B",
                name="B",
                tag="1",
                team="Blue",
                score=3600,
                kills=14,
                deaths=22,
                assists=2,
                agent="Sage",
                damage_made=2800,
                damage_received=4100,
                headshots=10,
                bodyshots=40,
                legshots=5,
                multikills_2k=1,
                multikills_3k=0,
                multikills_4k=0,
                multikills_5k=0,
                first_kills=2,
                first_deaths=4,
                kast_rounds=12,
            ),
        ),
        rounds_red=13,
        rounds_blue=11,
    )


def test_build_extended_stats_links_puuid_to_user_id_and_computes_rating():
    from services.match_verifier import build_extended_stats

    summary = _summary_with_two_players()
    extended = build_extended_stats(
        summary,
        puuid_to_user_id={"P-A": "uid-A", "P-B": "uid-B"},
        queue_type="pro",
    )
    by_uid = {x.user_id: x for x in extended}
    assert "uid-A" in by_uid
    assert by_uid["uid-A"].kills == 22
    assert by_uid["uid-A"].queue_type == "pro"
    assert by_uid["uid-A"].map_name == "Ascent"
    assert by_uid["uid-A"].agent == "Jett"
    assert by_uid["uid-A"].win is True
    assert by_uid["uid-B"].win is False
    assert by_uid["uid-A"].rating_2_0 > by_uid["uid-B"].rating_2_0


def test_build_extended_stats_skips_unmapped_puuids():
    from services.match_verifier import build_extended_stats

    summary = _summary_with_two_players()
    extended = build_extended_stats(
        summary,
        puuid_to_user_id={"P-A": "uid-A"},
        queue_type="open",
    )
    assert len(extended) == 1
    assert extended[0].user_id == "uid-A"


def test_build_extended_stats_acs_uses_rounds_played():
    from services.match_verifier import build_extended_stats

    summary = _summary_with_two_players()
    extended = build_extended_stats(
        summary,
        puuid_to_user_id={"P-A": "uid-A"},
        queue_type="pro",
    )
    # Combat score 5400 / 24 rounds = 225 ACS
    assert extended[0].acs == pytest.approx(225.0)


# ── ratings_by_uid (feeds pro-queue ELO weighting) ────────────────
def test_ratings_by_uid_maps_and_computes():
    from services.match_verifier import build_extended_stats, ratings_by_uid

    summary = _summary_with_two_players()
    puuid_map = {"P-A": "uid-A", "P-B": "uid-B"}
    ratings = ratings_by_uid(summary, puuid_map)

    # Same Rating 2.0 values as build_extended_stats, keyed by user_id.
    extended = {
        x.user_id: x
        for x in build_extended_stats(summary, puuid_to_user_id=puuid_map, queue_type="pro")
    }
    assert ratings["uid-A"] == pytest.approx(extended["uid-A"].rating_2_0)
    assert ratings["uid-B"] == pytest.approx(extended["uid-B"].rating_2_0)


def test_ratings_by_uid_skips_unmapped():
    from services.match_verifier import ratings_by_uid

    summary = _summary_with_two_players()
    ratings = ratings_by_uid(summary, {"P-A": "uid-A"})
    assert set(ratings) == {"uid-A"}


def test_ratings_by_uid_short_match_returns_empty():
    """Below the min-rounds guard (forfeits), no weighting -> {} (flat)."""
    from services.match_verifier import ratings_by_uid
    from services.riot_api import MatchPlayerStats, MatchSummary

    short = MatchSummary(
        matchid="m-short",
        mode="Custom Game",
        map_name="Ascent",
        started_at=datetime(2026, 6, 1, tzinfo=UTC),
        rounds_played=4,  # < ELO_MIN_ROUNDS_FOR_WEIGHT (6)
        players=(
            MatchPlayerStats(
                puuid="P-A",
                name="A",
                tag="1",
                team="Red",
                score=900,
                kills=4,
                deaths=1,
                assists=1,
                agent="Jett",
                damage_made=600,
                damage_received=200,
                headshots=3,
                bodyshots=8,
                legshots=1,
                multikills_2k=0,
                multikills_3k=0,
                multikills_4k=0,
                multikills_5k=0,
                first_kills=1,
                first_deaths=0,
                kast_rounds=4,
            ),
        ),
        rounds_red=4,
        rounds_blue=0,
    )
    assert ratings_by_uid(short, {"P-A": "uid-A"}) == {}
