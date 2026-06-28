"""Tests for Rating 2.0 stats persistence in services/repository.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_insert_match_player_stats_persists_docs(mongo_db):
    from services.repository import insert_match_player_stats

    docs = [
        {
            "_id": "match-1:user-A",
            "match_id": "match-1",
            "user_id": "A",
            "queue_type": "open",
            "map": "Ascent",
            "agent": "Jett",
            "rounds_played": 24,
            "win": True,
            "kills": 22,
            "deaths": 14,
            "assists": 5,
            "damage_made": 4123,
            "damage_received": 3580,
            "headshots": 18,
            "bodyshots": 50,
            "legshots": 4,
            "multikills_2k": 3,
            "multikills_3k": 1,
            "multikills_4k": 0,
            "multikills_5k": 0,
            "first_kills": 4,
            "first_deaths": 2,
            "kast_rounds": 19,
            "acs": 287,
            "rating_2_0": 1.34,
            "created_at": datetime.now(UTC),
        }
    ]
    n = insert_match_player_stats(mongo_db, docs)
    assert n == 1
    saved = mongo_db["match_player_stats"].find_one({"_id": "match-1:user-A"})
    assert saved["rating_2_0"] == 1.34


def test_insert_match_player_stats_is_idempotent_on_duplicate(mongo_db):
    """Re-inserting the same _id must return 0 inserted and not crash."""
    from services.repository import insert_match_player_stats

    doc = {
        "_id": "match-2:user-B",
        "match_id": "match-2",
        "user_id": "B",
        "queue_type": "open",
        "rounds_played": 24,
        "kills": 1,
        "deaths": 0,
        "assists": 0,
        "damage_made": 100,
        "kast_rounds": 1,
        "rating_2_0": 0.5,
        "created_at": datetime.now(UTC),
    }
    assert insert_match_player_stats(mongo_db, [doc]) == 1
    assert insert_match_player_stats(mongo_db, [doc]) == 0


def test_insert_match_player_stats_partial_dup_inserts_new(mongo_db):
    """When a batch has some duplicates and some new docs, the new ones must still land."""
    from services.repository import insert_match_player_stats

    base = {
        "match_id": "m",
        "queue_type": "advanced",
        "rounds_played": 24,
        "kills": 0,
        "deaths": 0,
        "assists": 0,
        "damage_made": 0,
        "kast_rounds": 0,
        "rating_2_0": 0.0,
        "created_at": datetime.now(UTC),
    }
    assert (
        insert_match_player_stats(
            mongo_db,
            [
                {**base, "_id": "m:U1", "user_id": "U1"},
            ],
        )
        == 1
    )

    n = insert_match_player_stats(
        mongo_db,
        [
            {**base, "_id": "m:U1", "user_id": "U1"},
            {**base, "_id": "m:U2", "user_id": "U2"},
        ],
    )
    assert n == 1
    assert mongo_db["match_player_stats"].count_documents({}) == 2


def test_insert_match_player_stats_empty_list_is_noop(mongo_db):
    from services.repository import insert_match_player_stats

    assert insert_match_player_stats(mongo_db, []) == 0


def test_update_rating_aggregates_upserts_and_increments(mongo_db):
    from services.repository import update_rating_aggregates

    delta = {
        "user_id": "U1",
        "queue_type": "open",
        "games": 1,
        "rounds_played": 24,
        "kills": 22,
        "deaths": 14,
        "assists": 5,
        "damage_made": 4123,
        "damage_received": 3580,
        "headshots": 18,
        "bodyshots": 50,
        "legshots": 4,
        "multikills_2k": 3,
        "multikills_3k": 1,
        "multikills_4k": 0,
        "multikills_5k": 0,
        "first_kills": 4,
        "first_deaths": 2,
        "kast_rounds": 19,
        "rating_2_0_sum": 1.34,
    }
    update_rating_aggregates(mongo_db, [delta])
    doc = mongo_db["player_rating_aggregates"].find_one({"_id": "U1:open"})
    assert doc["games"] == 1
    assert doc["kills"] == 22
    assert doc["damage_made"] == 4123
    assert "updated_at" in doc


def test_update_rating_aggregates_accumulates_on_second_call(mongo_db):
    from services.repository import update_rating_aggregates

    base = {
        "user_id": "U2",
        "queue_type": "open",
        "games": 1,
        "rounds_played": 20,
        "kills": 10,
        "deaths": 10,
        "assists": 2,
        "damage_made": 2000,
        "damage_received": 2000,
        "headshots": 5,
        "bodyshots": 30,
        "legshots": 1,
        "multikills_2k": 1,
        "multikills_3k": 0,
        "multikills_4k": 0,
        "multikills_5k": 0,
        "first_kills": 1,
        "first_deaths": 1,
        "kast_rounds": 12,
        "rating_2_0_sum": 1.0,
    }
    update_rating_aggregates(mongo_db, [base])
    update_rating_aggregates(mongo_db, [base])
    doc = mongo_db["player_rating_aggregates"].find_one({"_id": "U2:open"})
    assert doc["games"] == 2
    assert doc["kills"] == 20
    assert doc["kast_rounds"] == 24
    assert doc["rating_2_0_sum"] == pytest.approx(2.0)


def test_get_rating_aggregate_returns_none_when_missing(mongo_db):
    from services.repository import get_rating_aggregate

    assert get_rating_aggregate(mongo_db, user_id="missing", queue_type="open") is None


def test_get_rating_aggregate_returns_doc(mongo_db):
    from services.repository import get_rating_aggregate, update_rating_aggregates

    update_rating_aggregates(
        mongo_db,
        [
            {
                "user_id": "U3",
                "queue_type": "advanced",
                "games": 1,
                "rounds_played": 24,
                "kills": 0,
                "deaths": 0,
                "assists": 0,
                "damage_made": 0,
                "damage_received": 0,
                "headshots": 0,
                "bodyshots": 0,
                "legshots": 0,
                "multikills_2k": 0,
                "multikills_3k": 0,
                "multikills_4k": 0,
                "multikills_5k": 0,
                "first_kills": 0,
                "first_deaths": 0,
                "kast_rounds": 0,
                "rating_2_0_sum": 0.1587,
            }
        ],
    )
    doc = get_rating_aggregate(mongo_db, user_id="U3", queue_type="advanced")
    assert doc is not None
    assert doc["games"] == 1
