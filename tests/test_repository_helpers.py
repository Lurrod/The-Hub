"""Tests for compound _id helpers in services/repository.py."""

import pytest

from services.repository import (
    QUEUE_TYPES,
    active_queue_id,
    is_valid_queue_type,
    leaderboard_state_id,
    player_doc_id,
)


def test_queue_types_constant():
    assert QUEUE_TYPES == ("open", "advanced")


def test_is_valid_queue_type():
    assert is_valid_queue_type("open")
    assert is_valid_queue_type("advanced")
    assert not is_valid_queue_type("pro")
    assert not is_valid_queue_type("gc")
    assert not is_valid_queue_type("OPEN")
    assert not is_valid_queue_type("")
    assert not is_valid_queue_type("ranked")


def test_player_doc_id():
    assert player_doc_id(123, "open") == "123:open"
    assert player_doc_id("456", "advanced") == "456:advanced"


def test_active_queue_id():
    assert active_queue_id("open") == "active:open"
    assert active_queue_id("advanced") == "active:advanced"


def test_leaderboard_state_id():
    assert leaderboard_state_id("open") == "current:open"
    assert leaderboard_state_id("advanced") == "current:advanced"


def test_leaderboard_state_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        leaderboard_state_id("ranked")


def test_player_doc_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        player_doc_id(123, "ranked")


def test_active_queue_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        active_queue_id("ranked")


import mongomock

from services.repository import get_or_create_player


def test_get_or_create_player_uses_compound_id():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo"]

    doc = get_or_create_player(
        col, user_id=1, queue_type="open", display_name="Alice", initial_elo=2000
    )
    assert doc["_id"] == "1:open"
    assert doc["elo"] == 2000
    assert doc["wins"] == 0
    assert doc["queue_type"] == "open"
    assert doc["name"] == "Alice"


def test_get_or_create_player_isolates_queue_types():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo"]
    get_or_create_player(col, user_id=1, queue_type="open", display_name="Alice", initial_elo=2000)
    get_or_create_player(
        col, user_id=1, queue_type="advanced", display_name="Alice", initial_elo=2000
    )
    docs = list(col.find())
    assert len(docs) == 2
    assert {d["_id"] for d in docs} == {"1:open", "1:advanced"}


from services.repository import (
    add_player_to_queue,
    close_active_queue,
    delete_active_queue,
    find_player_in_any_queue,
    get_active_queue,
    remove_player_from_queue,
    setup_active_queue,
)


def test_setup_and_get_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="advanced", channel_id=200, message_id=888)

    open_q = get_active_queue(db, guild_id=42, queue_type="open")
    advanced = get_active_queue(db, guild_id=42, queue_type="advanced")

    assert open_q["_id"] == "active:open"
    assert open_q["channel_id"] == 100
    assert open_q["queue_type"] == "open"
    assert advanced["_id"] == "active:advanced"
    assert advanced["channel_id"] == 200
    assert advanced["queue_type"] == "advanced"


def test_add_remove_player_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=100, message_id=999)

    res = add_player_to_queue(db, guild_id=42, queue_type="open", user_id=1)
    assert res.success
    assert res.queue["players"] == ["1"]
    assert res.queue["queue_type"] == "open"

    res = remove_player_from_queue(db, guild_id=42, queue_type="open", user_id=1)
    assert res.success
    assert res.queue["players"] == []


def test_find_player_in_any_queue():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=200, message_id=888)
    add_player_to_queue(db, guild_id=42, queue_type="open", user_id=1)

    assert find_player_in_any_queue(db, guild_id=42, user_id=1) == "open"
    assert find_player_in_any_queue(db, guild_id=42, user_id=2) is None


def test_delete_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="advanced", channel_id=200, message_id=888)

    assert delete_active_queue(db, guild_id=42, queue_type="open") is True
    assert get_active_queue(db, guild_id=42, queue_type="open") is None
    assert get_active_queue(db, guild_id=42, queue_type="advanced") is not None


def test_close_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="open", channel_id=100, message_id=999)
    close_active_queue(db, guild_id=42, queue_type="open")
    open_q = get_active_queue(db, guild_id=42, queue_type="open")
    assert open_q["status"] == "forming"


from services.repository import (
    clear_leaderboard_message_id,
    get_leaderboard_message_id,
    set_leaderboard_message_id,
)


def test_leaderboard_message_id_per_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    set_leaderboard_message_id(db, guild_id=42, queue_type="open", message_id=111)
    set_leaderboard_message_id(db, guild_id=42, queue_type="advanced", message_id=222)

    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") == 111
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="advanced") == 222

    clear_leaderboard_message_id(db, guild_id=42, queue_type="open")
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") is None
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="advanced") == 222


from services.repository import create_match, get_match


def test_create_match_persists_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    match_id = create_match(
        db,
        origin_guild_id=42,
        queue_type="open",
        team_a=[{"id": "1", "name": "A", "elo": 2000}],
        team_b=[{"id": "2", "name": "B", "elo": 2000}],
        map_name="Ascent",
        lobby_leader_id=1,
        category_name="Match #1",
        message_id=999,
        channel_id=100,
    )
    doc = get_match(db, match_id=match_id)
    assert doc["queue_type"] == "open"


def test_add_match_vote_coerces_user_id_to_numeric_field():
    """The `votes` key is normalized numerically: an int id and its str
    equivalent produce the same key, and a non-numeric id is rejected
    before reaching Mongo (anti field-path injection)."""
    from services.repository import add_match_vote

    db = mongomock.MongoClient(tz_aware=True).db
    match_id = create_match(
        db,
        origin_guild_id=42,
        queue_type="open",
        team_a=[{"id": "1", "name": "A", "elo": 2000}],
        team_b=[{"id": "2", "name": "B", "elo": 2000}],
        map_name="Ascent",
        lobby_leader_id=1,
        category_name="Match #1",
        message_id=999,
        channel_id=100,
    )

    # int and numeric str -> same key "3" (idempotent on re-vote).
    add_match_vote(db, match_id, 3, "a")
    assert get_match(db, match_id=match_id)["votes"] == {"3": "a"}
    add_match_vote(db, match_id, "3", "b")
    assert get_match(db, match_id=match_id)["votes"] == {"3": "b"}

    # A non-numeric id (e.g. attempted path injection) is rejected.
    with pytest.raises(TypeError, match="user_id"):
        add_match_vote(db, match_id, "evil.$gt", "a")


def test_reserve_match_number_starts_at_one(mongo_db):
    from services.repository import reserve_match_number

    n = reserve_match_number(mongo_db, guild_id=111)
    assert n == 1


def test_reserve_match_number_monotonic(mongo_db):
    from services.repository import reserve_match_number

    assert reserve_match_number(mongo_db, guild_id=222) == 1
    assert reserve_match_number(mongo_db, guild_id=222) == 2
    assert reserve_match_number(mongo_db, guild_id=222) == 3


def test_reserve_match_number_independent_between_guilds(mongo_db):
    from services.repository import reserve_match_number

    assert reserve_match_number(mongo_db, guild_id=333) == 1
    assert reserve_match_number(mongo_db, guild_id=444) == 1
    assert reserve_match_number(mongo_db, guild_id=333) == 2


def test_to_int_id_accepts_int_and_numeric_str():
    from services.repository import _to_int_id

    assert _to_int_id(123) == 123
    assert _to_int_id("456") == 456


def test_to_int_id_raises_with_field_context_on_garbage():
    from services.repository import _to_int_id

    with pytest.raises(TypeError, match="user_id"):
        _to_int_id("Bob", field="user_id")
    with pytest.raises(TypeError, match="channel_id"):
        _to_int_id(None, field="channel_id")


def test_mark_match_cleanup_started_sets_delete_started_at(mongo_db):
    from services.repository import (
        get_matches_col,
        mark_match_cleanup_started,
    )

    matches = get_matches_col(mongo_db)
    res = matches.insert_one({"origin_guild_id": 1, "status": "validated_a", "category_id": 555})

    mark_match_cleanup_started(mongo_db, res.inserted_id)

    doc = matches.find_one({"_id": res.inserted_id})
    assert "delete_started_at" in doc
    assert doc["delete_started_at"] is not None


def test_find_category_ids_with_cleanup_started_filters_by_guild(mongo_db):
    from datetime import UTC, datetime

    from services.repository import (
        find_category_ids_with_cleanup_started,
        get_matches_col,
        mark_match_cleanup_started,
    )

    matches = get_matches_col(mongo_db)
    a = matches.insert_one(
        {"origin_guild_id": 1, "status": "validated_a", "category_id": 100}
    ).inserted_id
    b = matches.insert_one(
        {"origin_guild_id": 1, "status": "pending", "category_id": 200}
    ).inserted_id
    matches.insert_one({"origin_guild_id": 2, "status": "validated_a", "category_id": 300})
    # Match from another guild marked cleanup -> must NOT appear.
    c = matches.insert_one(
        {"origin_guild_id": 2, "status": "pending", "category_id": 400}
    ).inserted_id
    matches.update_one({"_id": c}, {"$set": {"delete_started_at": datetime.now(UTC)}})

    mark_match_cleanup_started(mongo_db, a)
    mark_match_cleanup_started(mongo_db, b)

    result = find_category_ids_with_cleanup_started(mongo_db, origin_guild_id=1)
    assert result == {100, 200}


def test_find_category_ids_with_cleanup_started_excludes_unmarked(mongo_db):
    from services.repository import (
        find_category_ids_with_cleanup_started,
        get_matches_col,
    )

    get_matches_col(mongo_db).insert_one(
        {"origin_guild_id": 5, "status": "pending", "category_id": 777}
    )
    # No delete_started_at => not returned even if active.
    assert find_category_ids_with_cleanup_started(mongo_db, origin_guild_id=5) == set()


# -- expire_stale_contested --
# Safety net: a "contested" match stays in the find_active_match_for_player
# gate as long as no admin resolves it. If an admin applies the ELO via
# /win + /lose without touching the match doc, the 10 players are
# blocked indefinitely. expire_stale_contested automatically transitions
# contested matches older than cutoff_dt to cleaned_up.


def test_expire_stale_contested_marks_old_matches_cleaned_up(mongo_db):
    from datetime import UTC, datetime, timedelta

    from services.repository import expire_stale_contested, get_matches_col

    now = datetime.now(UTC)
    old = now - timedelta(hours=25)
    matches = get_matches_col(mongo_db)
    mid = matches.insert_one(
        {"origin_guild_id": 1, "status": "contested", "created_at": old}
    ).inserted_id

    n = expire_stale_contested(mongo_db, origin_guild_id=1, cutoff_dt=now - timedelta(hours=24))

    assert n == 1
    doc = matches.find_one({"_id": mid})
    assert doc["status"] == "cleaned_up"
    assert doc.get("cleaned_up_at") is not None
    assert doc.get("cleaned_up_by") == "auto_expire_contested"


def test_expire_stale_contested_skips_recent_matches(mongo_db):
    from datetime import UTC, datetime, timedelta

    from services.repository import expire_stale_contested, get_matches_col

    now = datetime.now(UTC)
    recent = now - timedelta(hours=12)
    matches = get_matches_col(mongo_db)
    mid = matches.insert_one(
        {"origin_guild_id": 1, "status": "contested", "created_at": recent}
    ).inserted_id

    n = expire_stale_contested(mongo_db, origin_guild_id=1, cutoff_dt=now - timedelta(hours=24))

    assert n == 0
    doc = matches.find_one({"_id": mid})
    assert doc["status"] == "contested"


def test_expire_stale_contested_skips_non_contested_statuses(mongo_db):
    from datetime import UTC, datetime, timedelta

    from services.repository import expire_stale_contested, get_matches_col

    now = datetime.now(UTC)
    old = now - timedelta(hours=48)
    matches = get_matches_col(mongo_db)
    matches.insert_one({"origin_guild_id": 1, "status": "pending", "created_at": old})
    matches.insert_one({"origin_guild_id": 1, "status": "validated_a", "created_at": old})
    matches.insert_one({"origin_guild_id": 1, "status": "validated_b", "created_at": old})
    matches.insert_one({"origin_guild_id": 1, "status": "cancelled", "created_at": old})

    n = expire_stale_contested(mongo_db, origin_guild_id=1, cutoff_dt=now - timedelta(hours=24))

    assert n == 0
    # No doc must have changed status.
    statuses = {d["status"] for d in matches.find({})}
    assert statuses == {"pending", "validated_a", "validated_b", "cancelled"}


def test_expire_stale_contested_scopes_by_guild(mongo_db):
    from datetime import UTC, datetime, timedelta

    from services.repository import expire_stale_contested, get_matches_col

    now = datetime.now(UTC)
    old = now - timedelta(hours=48)
    matches = get_matches_col(mongo_db)
    g1 = matches.insert_one(
        {"origin_guild_id": 1, "status": "contested", "created_at": old}
    ).inserted_id
    g2 = matches.insert_one(
        {"origin_guild_id": 2, "status": "contested", "created_at": old}
    ).inserted_id

    n = expire_stale_contested(mongo_db, origin_guild_id=1, cutoff_dt=now - timedelta(hours=24))

    assert n == 1
    assert matches.find_one({"_id": g1})["status"] == "cleaned_up"
    # Guild 2 NOT touched.
    assert matches.find_one({"_id": g2})["status"] == "contested"


def test_expire_stale_contested_returns_count_for_multiple_docs(mongo_db):
    from datetime import UTC, datetime, timedelta

    from services.repository import expire_stale_contested, get_matches_col

    now = datetime.now(UTC)
    old = now - timedelta(hours=30)
    matches = get_matches_col(mongo_db)
    matches.insert_one({"origin_guild_id": 1, "status": "contested", "created_at": old})
    matches.insert_one({"origin_guild_id": 1, "status": "contested", "created_at": old})
    matches.insert_one({"origin_guild_id": 1, "status": "contested", "created_at": old})
    # One recent, must be skipped.
    matches.insert_one(
        {"origin_guild_id": 1, "status": "contested", "created_at": now - timedelta(hours=2)}
    )

    n = expire_stale_contested(mongo_db, origin_guild_id=1, cutoff_dt=now - timedelta(hours=24))

    assert n == 3


def test_create_preparing_match_inserts_doc_with_status_preparing(mongo_db):
    from services.repository import create_preparing_match, get_matches_col

    match_id = create_preparing_match(
        mongo_db,
        queue_type="open",
        origin_guild_id=42,
        match_number=7,
        category_id=1234,
        channel_id=5678,
        player_ids=[100, 200, 300],
    )

    doc = get_matches_col(mongo_db).find_one({"_id": match_id})
    assert doc is not None
    assert doc["status"] == "preparing"
    assert doc["queue_type"] == "open"
    assert doc["origin_guild_id"] == 42
    assert doc["match_number"] == 7
    assert doc["category_id"] == 1234
    assert doc["channel_id"] == 5678
    assert doc["player_ids"] == [100, 200, 300]
    assert doc["team_a"] is None
    assert doc["team_b"] is None
    assert doc["map"] is None


def test_finalize_preparing_match_promotes_to_pending(mongo_db):
    from services.repository import (
        create_preparing_match,
        finalize_preparing_match,
        get_matches_col,
    )

    match_id = create_preparing_match(
        mongo_db,
        queue_type="advanced",
        origin_guild_id=42,
        match_number=8,
        category_id=1,
        channel_id=2,
        player_ids=[1, 2],
    )

    finalize_preparing_match(
        mongo_db,
        match_id,
        team_a=[{"id": 1}],
        team_b=[{"id": 2}],
        map_name="Ascent",
        lobby_leader_id=1,
        category_name="Match #8",
        team_a_side="Attack",
    )

    doc = get_matches_col(mongo_db).find_one({"_id": match_id})
    assert doc["status"] == "pending"
    assert doc["team_a"] == [{"id": 1}]
    assert doc["team_b"] == [{"id": 2}]
    assert doc["map"] == "Ascent"
    assert doc["lobby_leader_id"] == "1"
    assert doc["category_name"] == "Match #8"
    assert doc["team_a_side"] == "Attack"


def test_finalize_preparing_match_is_noop_when_already_promoted(mongo_db):
    from services.repository import (
        create_preparing_match,
        finalize_preparing_match,
        get_matches_col,
    )

    match_id = create_preparing_match(
        mongo_db,
        queue_type="open",
        origin_guild_id=42,
        match_number=9,
        category_id=1,
        channel_id=2,
        player_ids=[],
    )
    # Manually flip to cancelled so the CAS guard rejects promotion.
    get_matches_col(mongo_db).update_one({"_id": match_id}, {"$set": {"status": "cancelled"}})

    finalize_preparing_match(
        mongo_db,
        match_id,
        team_a=[],
        team_b=[],
        map_name="Bind",
        lobby_leader_id=1,
        category_name="Match #9",
    )

    doc = get_matches_col(mongo_db).find_one({"_id": match_id})
    assert doc["status"] == "cancelled"  # untouched


def test_cancel_preparing_match_atomic_transition(mongo_db):
    from services.repository import (
        cancel_preparing_match,
        create_preparing_match,
        get_matches_col,
    )

    match_id = create_preparing_match(
        mongo_db,
        queue_type="advanced",
        origin_guild_id=42,
        match_number=10,
        category_id=1,
        channel_id=2,
        player_ids=[],
    )

    before = cancel_preparing_match(mongo_db, match_id)
    assert before is not None
    assert before["status"] == "preparing"

    doc = get_matches_col(mongo_db).find_one({"_id": match_id})
    assert doc["status"] == "cancelled"

    # Idempotent: second call returns None (already cancelled).
    again = cancel_preparing_match(mongo_db, match_id)
    assert again is None


def test_cancel_match_atomically_accepts_preparing_status(mongo_db):
    from services.repository import (
        cancel_match_atomically,
        create_preparing_match,
        get_matches_col,
    )

    match_id = create_preparing_match(
        mongo_db,
        queue_type="open",
        origin_guild_id=42,
        match_number=11,
        category_id=1,
        channel_id=9999,
        player_ids=[],
    )

    before = cancel_match_atomically(mongo_db, channel_id=9999)
    assert before is not None
    assert before["_id"] == match_id
    assert before["status"] == "preparing"

    doc = get_matches_col(mongo_db).find_one({"_id": match_id})
    assert doc["status"] == "cancelled"


def test_find_preparing_matches_returns_only_preparing(mongo_db):
    from services.repository import (
        create_preparing_match,
        find_preparing_matches,
        get_matches_col,
    )

    m1 = create_preparing_match(
        mongo_db,
        queue_type="open",
        origin_guild_id=42,
        match_number=12,
        category_id=1,
        channel_id=1,
        player_ids=[],
    )
    m2 = create_preparing_match(
        mongo_db,
        queue_type="advanced",
        origin_guild_id=42,
        match_number=13,
        category_id=2,
        channel_id=2,
        player_ids=[],
    )
    # Flip one to cancelled to ensure it's excluded.
    get_matches_col(mongo_db).update_one({"_id": m2}, {"$set": {"status": "cancelled"}})

    found = find_preparing_matches(mongo_db)
    found_ids = {d["_id"] for d in found}
    assert m1 in found_ids
    assert m2 not in found_ids
