"""Centralized MongoDB access. All collections go through here."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database

logger = logging.getLogger(__name__)


# Ordered tuple of supported queue types. Order drives display
# (/setup, queue views): Open first, then Advanced, then Draft.
QUEUE_TYPES: tuple[str, ...] = ("open", "advanced", "draft")

# Queues that carry ELO + a leaderboard. "draft" is intentionally
# excluded: it only forms teams and posts the end-of-game scoreboard,
# without points or a ranking.
RANKED_QUEUE_TYPES: tuple[str, ...] = ("open", "advanced")


def is_valid_queue_type(queue_type: str) -> bool:
    return queue_type in QUEUE_TYPES


def _check_queue_type(queue_type: str) -> None:
    if not is_valid_queue_type(queue_type):
        raise ValueError(f"invalid queue_type: {queue_type!r}. Expected: {QUEUE_TYPES}")


# This module's signatures accept `int | str` for Discord IDs
# (user_id, guild_id, channel_id, role_id, message_id, category_id) because:
#   - Discord.py provides ints (`member.id`, `guild.id`, ...).
#   - Some legacy Mongo docs store the ID as str; we read them as-is and
#     pass them back to the repo without a cast.
# Concrete risk: a caller passes `member.name` by mistake instead of
# `member.id` -> `int("Bob")` raises a bare `ValueError`. This helper
# keeps the coercion centralized and gives a message that points to the
# field and the offending value.
def _to_int_id(value: int | str, *, field: str = "id") -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field}: expected int or numeric str, received {value!r}") from exc


def player_doc_id(user_id: int | str, queue_type: str) -> str:
    """Compound _id for a player doc in the shared `elo` collection."""
    _check_queue_type(queue_type)
    return f"{user_id}:{queue_type}"


def active_queue_id(queue_type: str) -> str:
    """_id for the active queue of a given type in queue_<guild>."""
    _check_queue_type(queue_type)
    return f"active:{queue_type}"


def leaderboard_state_id(queue_type: str) -> str:
    """_id for the leaderboard state of a type in leaderboard_state_<guild>."""
    _check_queue_type(queue_type)
    return f"current:{queue_type}"


# Cache of already-indexed collections to avoid re-issuing create_index on
# every call (idempotent on the Mongo side, but useless perf-wise).
_indexed_collections: set[str] = set()


def _ensure_indexes(col, kind: str) -> None:
    """Create missing indexes on a collection. Idempotent and safe on
    failure (e.g. mongomock partial support, missing perms)."""
    name = col.full_name if hasattr(col, "full_name") else f"{kind}:{id(col)}"
    if name in _indexed_collections:
        return
    try:
        if kind == "elo":
            # Leaderboard sort by ELO desc.
            col.create_index([("elo", -1)])
        elif kind == "matches":
            # Vote message lookup + timeout scan + ELO verification scan.
            col.create_index([("message_id", 1)])
            col.create_index([("status", 1), ("created_at", 1)])
            col.create_index([("status", 1), ("validated_at", 1), ("elo_applied", 1)])
        elif kind == "riot":
            # PUUID dedup: prevents the same Riot account from being linked
            # to 2 Discord accounts (multi-account farming of the ELO seed).
            col.create_index([("puuid", 1)], unique=True, sparse=True)
        elif kind == "rules":
            # Accessed only by _id (user_id), indexed by MongoDB by default.
            # No application-level index to create: explicit branch to
            # remove ambiguity (otherwise "rules" would fall into the
            # silent no-op of the if/elif and a future dev might think it
            # was forgotten).
            pass
        elif kind == "match_player_stats":
            # Leaderboard queries: lookup by user/queue/date, sorted by date desc.
            col.create_index([("user_id", 1), ("queue_type", 1), ("created_at", -1)])
        elif kind == "player_rating_aggregates":
            # PK lookups only; no secondary index needed.
            pass
    except Exception as e:
        logger.error(f"[repository] _ensure_indexes({kind}) raised: {e}", exc_info=True)
    _indexed_collections.add(name)


def get_elo_col(db: Database) -> Collection:
    """ELO collection shared across all guilds.

    The `_id` doc stays compound `<user_id>:<queue_type>`. All bots using
    the same MongoDB read/write here, regardless of the originating
    Discord guild.
    """
    col = db["elo"]
    _ensure_indexes(col, "elo")
    return col


def get_bypass_col(db: Database) -> Collection:
    return db["bypass"]


def get_bypass_role(db: Database, guild_id: int | str) -> int | None:
    doc = get_bypass_col(db).find_one({"_id": str(guild_id)})
    return doc["role_id"] if doc else None


def set_bypass_role(db: Database, guild_id: int | str, role_id: int) -> None:
    get_bypass_col(db).update_one(
        {"_id": str(guild_id)},
        {"$set": {"role_id": role_id}},
        upsert=True,
    )


def get_or_create_player(
    col,
    user_id: int | str,
    queue_type: str,
    display_name: str,
    initial_elo: int = 2000,
) -> Mapping[str, Any]:
    """Atomically get or create a player's queue doc.

    The `_id` is `<user_id>:<queue_type>` (compound). The `queue_type`
    field is also persisted to allow filters by type (leaderboard,
    /reset-queue) without a regex on _id."""
    _check_queue_type(queue_type)
    doc_id = player_doc_id(user_id, queue_type)
    return col.find_one_and_update(
        {"_id": doc_id},
        {
            "$set": {"name": display_name},
            "$setOnInsert": {
                "elo": initial_elo,
                "wins": 0,
                "losses": 0,
                "queue_type": queue_type,
                "user_id": str(user_id),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


# -- V2: linked Riot accounts --------------------------------------
def get_riot_col(db: Database) -> Collection:
    """Riot link collection shared across all guilds."""
    col = db["riot"]
    _ensure_indexes(col, "riot")
    return col


def find_riot_account_by_puuid(
    db: Database,
    puuid: str,
) -> Mapping[str, Any] | None:
    """Returns the riot_account doc with this puuid, or None.

    Used for PUUID dedup in /link-riot: prevents the same Riot account
    from being linked to two different Discord accounts (multi-account)."""
    if not puuid:
        return None
    return get_riot_col(db).find_one({"puuid": puuid})


def link_riot_account(
    db: Database,
    user_id: int | str,
    *,
    riot_name: str,
    riot_tag: str,
    riot_region: str,
    puuid: str,
    peak_elo: int,
    source: str,
) -> None:
    """Record or update the Discord <-> Riot link (metadata only).

    The matchmaking ELO is stored in the shared `elo` collection; this
    doc is now only used to (a) verify that a player is linked to join
    the queue, (b) display the reference Riot rank.
    """
    from datetime import datetime

    get_riot_col(db).update_one(
        {"_id": str(user_id)},
        {
            "$set": {
                "riot_name": riot_name,
                "riot_tag": riot_tag,
                "riot_region": riot_region,
                "puuid": puuid,
                "peak_elo": peak_elo,
                "source": source,
                "fetched_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )


def get_riot_account(db: Database, user_id: int | str) -> Mapping[str, Any] | None:
    return get_riot_col(db).find_one({"_id": str(user_id)})


def unlink_riot_account(db: Database, user_id: int | str) -> bool:
    """Returns True if an entry was deleted."""
    res = get_riot_col(db).delete_one({"_id": str(user_id)})
    return res.deleted_count > 0


# -- Rules (rules acceptance) --------------------------------------
def get_rules_col(db: Database) -> Collection:
    """Global rules acceptance collection (key = user_id).

    Acceptance is per player (not per guild or queue), valid once and
    for all."""
    col = db["rules"]
    _ensure_indexes(col, "rules")
    return col


def has_accepted_rules(db: Database, user_id: int | str) -> bool:
    """True if the player has already accepted the rules."""
    return get_rules_col(db).find_one({"_id": str(user_id)}) is not None


def record_rules_acceptance(db: Database, user_id: int | str, *, display_name: str) -> None:
    """Record (or update) the rules acceptance. Idempotent:
    a re-click simply rewrites accepted_at."""
    get_rules_col(db).update_one(
        {"_id": str(user_id)},
        {"$set": {"accepted_at": datetime.now(UTC), "display_name": display_name}},
        upsert=True,
    )


# -- V2: 10-mans queue ---------------------------------------------
QUEUE_SIZE_DEFAULT = 10


def get_queue_col(db: Database, guild_id: int | str) -> Collection:
    return db[f"queue_{guild_id}"]


def get_active_queue(
    db: Database, guild_id: int | str, queue_type: str
) -> Mapping[str, Any] | None:
    _check_queue_type(queue_type)
    return get_queue_col(db, guild_id).find_one({"_id": active_queue_id(queue_type)})


def setup_active_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    channel_id: int,
    message_id: int,
) -> None:
    """Create (or replace) the active queue of this type for this guild."""
    _check_queue_type(queue_type)
    from datetime import datetime

    get_queue_col(db, guild_id).update_one(
        {"_id": active_queue_id(queue_type)},
        {
            "$set": {
                "channel_id": _to_int_id(channel_id, field="channel_id"),
                "message_id": _to_int_id(message_id, field="message_id"),
                "players": [],
                "status": "open",
                "queue_type": queue_type,
                "created_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )


def delete_active_queue(db: Database, guild_id: int | str, queue_type: str) -> bool:
    _check_queue_type(queue_type)
    res = get_queue_col(db, guild_id).delete_one({"_id": active_queue_id(queue_type)})
    return res.deleted_count > 0


def close_active_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
) -> Mapping[str, Any] | None:
    """Mark the queue of this type as 'forming' and return the updated doc.

    Returns None if the queue does not exist. Uses find_one_and_update to
    merge write + read into a single atomic round-trip.
    """
    _check_queue_type(queue_type)
    return get_queue_col(db, guild_id).find_one_and_update(
        {"_id": active_queue_id(queue_type)},
        {"$set": {"status": "forming"}},
        return_document=ReturnDocument.AFTER,
    )


@dataclass(frozen=True)
class QueueResult:
    success: bool
    reason: str
    queue: Mapping[str, Any] | None


def add_player_to_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id: int | str,
    *,
    max_size: int = QUEUE_SIZE_DEFAULT,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
    if not queue:
        return QueueResult(False, "no_queue", None)
    if queue.get("status") != "open":
        return QueueResult(False, "queue_closed", queue)
    players = queue.get("players", [])
    uid_str = str(user_id)
    if uid_str in players:
        return QueueResult(False, "already_in", queue)
    if len(players) >= max_size:
        return QueueResult(False, "queue_full", queue)
    updated = col.find_one_and_update(
        {
            "_id": qid,
            "status": "open",
            "players": {"$nin": [uid_str]},
            "$expr": {
                "$lt": [
                    {"$size": {"$ifNull": ["$players", []]}},
                    max_size,
                ]
            },
        },
        {"$push": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        return QueueResult(False, "race", queue)
    return QueueResult(True, "added", updated)


def remove_player_from_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id: int | str,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
    if not queue:
        return QueueResult(False, "no_queue", None)
    uid_str = str(user_id)
    if uid_str not in queue.get("players", []):
        return QueueResult(False, "not_in", queue)
    updated = col.find_one_and_update(
        {"_id": qid},
        {"$pull": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    return QueueResult(True, "removed", updated)


def find_player_in_any_queue(
    db: Database,
    guild_id: int | str,
    user_id: int | str,
) -> str | None:
    """Returns the queue_type where the user is present, or None."""
    uid_str = str(user_id)
    col = get_queue_col(db, guild_id)
    for qt in QUEUE_TYPES:
        doc = col.find_one({"_id": active_queue_id(qt), "players": uid_str})
        if doc is not None:
            return qt
    return None


# -- V2: matches ---------------------------------------------------
def get_matches_col(db: Database) -> Collection:
    """Matches collection shared across all guilds.

    Each match carries an `origin_guild_id` field for traceability
    (present only on matches created after the refactor)."""
    col = db["matches"]
    _ensure_indexes(col, "matches")
    return col


def create_preparing_match(
    db: Database,
    *,
    queue_type: str,
    origin_guild_id: int,
    match_number: int,
    category_id: int,
    channel_id: int,
    player_ids: list[int],
) -> Any:
    """Insert a placeholder match doc with status='preparing'.

    Persisted BEFORE captain draft / map ban so that:
    - the match category is recognized at startup (not auto-deleted as
      an orphan)
    - `/match-cancel` and admin commands can resolve the match by
      channel_id even while the in-memory draft/ban session is running
    - if the bot restarts mid-draft/ban, startup recovery can detect
      these stuck matches and clean them up.

    `finalize_preparing_match` later promotes preparing -> pending once
    the teams and map are known.
    """
    _check_queue_type(queue_type)
    from datetime import datetime

    doc: dict[str, Any] = {
        "status": "preparing",
        "queue_type": queue_type,
        "origin_guild_id": _to_int_id(origin_guild_id, field="origin_guild_id"),
        "match_number": int(match_number),
        "category_id": _to_int_id(category_id, field="category_id"),
        "channel_id": _to_int_id(channel_id, field="channel_id"),
        "player_ids": [int(p) for p in player_ids],
        "created_at": datetime.now(UTC),
        "votes": {},
        "message_id": None,
        "team_a": None,
        "team_b": None,
        "map": None,
        "lobby_leader_id": None,
        "category_name": None,
        "validated_at": None,
    }
    res = get_matches_col(db).insert_one(doc)
    return res.inserted_id


def finalize_preparing_match(
    db: Database,
    match_id: Any,
    *,
    team_a: list[dict],
    team_b: list[dict],
    map_name: str,
    lobby_leader_id: int | str,
    category_name: str | None,
    team_a_side: str | None = None,
) -> None:
    """Promote a 'preparing' match to 'pending' once teams + map are
    known. No-op if the doc is no longer in 'preparing' state (e.g.
    admin cancelled it during draft/ban)."""
    from datetime import datetime

    get_matches_col(db).update_one(
        {"_id": match_id, "status": "preparing"},
        {
            "$set": {
                "status": "pending",
                "team_a": team_a,
                "team_b": team_b,
                "map": map_name,
                "team_a_side": team_a_side,
                "lobby_leader_id": str(lobby_leader_id),
                "category_name": category_name,
                "promoted_at": datetime.now(UTC),
            }
        },
    )


def cancel_preparing_match(db: Database, match_id: Any) -> Mapping[str, Any] | None:
    """Atomically transition preparing -> cancelled. Used by draft/ban
    cancel paths and by the startup recovery sweep. Returns the doc
    BEFORE the update, or None if it was no longer preparing."""
    return get_matches_col(db).find_one_and_update(
        {"_id": match_id, "status": "preparing"},
        {"$set": {"status": "cancelled"}},
        return_document=ReturnDocument.BEFORE,
    )


def find_preparing_matches(
    db: Database, *, origin_guild_id: int | None = None
) -> list[Mapping[str, Any]]:
    """Return all matches stuck in 'preparing' state. Used by startup
    recovery to find drafts/map-bans interrupted by a bot restart."""
    query: dict[str, Any] = {"status": "preparing"}
    if origin_guild_id is not None:
        query["origin_guild_id"] = _to_int_id(origin_guild_id, field="origin_guild_id")
    return list(get_matches_col(db).find(query))


def create_match(
    db: Database,
    *,
    queue_type: str,
    origin_guild_id: int,
    team_a: list[dict],
    team_b: list[dict],
    map_name: str,
    lobby_leader_id: int | str,
    category_name: str | None,
    category_id: int | None = None,
    match_number: int | None = None,
    message_id: int | None,
    channel_id: int | None,
) -> Any:
    """Insert a new match. Returns its _id (ObjectId).

    `queue_type` (kw-only): "pro" | "semipro" | "open" | "gc". Persisted on
    the doc to allow filters by type (leaderboard refresh, /reset-queue,
    Pro Queue Henrik skip).

    `origin_guild_id` (kw-only): originating Discord guild of the match,
    for cross-guild traceability (the `matches` collection is shared)."""
    _check_queue_type(queue_type)
    from datetime import datetime

    doc: dict[str, Any] = {
        "team_a": team_a,
        "team_b": team_b,
        "map": map_name,
        "queue_type": queue_type,
        "origin_guild_id": _to_int_id(origin_guild_id, field="origin_guild_id"),
        "lobby_leader_id": str(lobby_leader_id),
        "category_name": category_name,
        "category_id": _to_int_id(category_id, field="category_id")
        if category_id is not None
        else None,
        "match_number": int(match_number) if match_number is not None else None,
        "status": "pending",
        "votes": {},
        "created_at": datetime.now(UTC),
        "validated_at": None,
        "message_id": _to_int_id(message_id, field="message_id") if message_id else None,
        "channel_id": _to_int_id(channel_id, field="channel_id") if channel_id else None,
    }
    res = get_matches_col(db).insert_one(doc)
    return res.inserted_id


def get_match(db: Database, match_id: Any) -> Mapping[str, Any] | None:
    return get_matches_col(db).find_one({"_id": match_id})


def get_match_by_message(db: Database, message_id: int) -> Mapping[str, Any] | None:
    return get_matches_col(db).find_one({"message_id": _to_int_id(message_id, field="message_id")})


# Statuses for which a player is considered still engaged in a match.
# Used for the queue anti-duplicate gate: a player already in one of these
# matches cannot join a new queue.
#   - "pending"      : vote open, match not yet concluded
#   - "contested"    : vote timed out, awaiting admin resolution
#
# The "validated_a" / "validated_b" statuses are intentionally EXCLUDED
# from the gate: once the vote is resolved, the match is logically closed.
# Blocking the queue on "elo_applied != True" would let any Henrik failure
# (private tracker, wrong account played, API down) freeze the 10 players
# indefinitely. ELO distribution is an async job independent of the gate;
# _verify_match + the expire_stale_contested safety net do the rest.
_ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE: tuple[str, ...] = (
    "pending",
    "contested",
)


def find_active_match_for_player(db: Database, user_id: int | str) -> Mapping[str, Any] | None:
    """Returns the active match (non-terminal status, ELO not applied) the
    `user_id` belongs to, or None.

    Used by the queue to refuse rejoining until the player has closed
    their ongoing match (vote or admin /match-cancel)."""
    uid_int = _to_int_id(user_id, field="user_id")
    return get_matches_col(db).find_one(
        {
            "$or": [{"team_a.id": uid_int}, {"team_b.id": uid_int}],
            "status": {"$in": list(_ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE)},
            "elo_applied": {"$ne": True},
        },
        {"_id": 1, "status": 1, "match_number": 1, "category_id": 1},
    )


def expire_stale_contested(
    db: Database,
    *,
    origin_guild_id: int | str,
    cutoff_dt,
) -> int:
    """Safety net: transition `contested` -> `cleaned_up` for every match
    created before `cutoff_dt`.

    Without this, an unresolved contested (admin applies ELO via /win + /lose
    but forgets /match-cancel or /match-cleanup) blocks the 10 players in
    the find_active_match_for_player gate forever. Called at boot and on
    every tick by the MatchCog timeout-loop.

    Returns:
        Number of transitioned docs.
    """
    gid = _to_int_id(origin_guild_id, field="origin_guild_id")
    res = get_matches_col(db).update_many(
        {
            "origin_guild_id": gid,
            "status": "contested",
            "created_at": {"$lt": cutoff_dt},
        },
        {
            "$set": {
                "status": "cleaned_up",
                "cleaned_up_at": datetime.now(UTC),
                "cleaned_up_by": "auto_expire_contested",
            }
        },
    )
    return res.modified_count


def add_match_vote(
    db: Database,
    match_id: Any,
    user_id: int | str,
    choice: str,
) -> Mapping[str, Any] | None:
    """Record/overwrite a user's vote. Returns the doc after the update.

    CAS on `status: pending`: prevents late votes on a match already
    cancelled, contested or validated. A latecomer who clicks while the
    match is cancelled records nothing (None) instead of polluting `votes`
    after the fact."""
    if choice not in ("a", "b"):
        raise ValueError(f"choice must be 'a' or 'b', received {choice!r}")
    # Centralized coercion as everywhere else in this module: guarantees a
    # numeric field key (`votes.<id>`) and rejects any non-numeric id
    # before it reaches Mongo. Without this, a `user_id` containing `.`/`$`
    # would be interpreted as a nested field path (CWE-943).
    uid = _to_int_id(user_id, field="user_id")
    return get_matches_col(db).find_one_and_update(
        {"_id": match_id, "status": "pending"},
        {"$set": {f"votes.{uid}": choice}},
        return_document=ReturnDocument.AFTER,
    )


def set_match_status(
    db: Database,
    match_id: Any,
    status: str,
) -> None:
    from datetime import datetime

    update: dict[str, Any] = {"status": status}
    if status in ("validated_a", "validated_b"):
        update["validated_at"] = datetime.now(UTC)
    get_matches_col(db).update_one({"_id": match_id}, {"$set": update})


def transition_match_status(
    db: Database,
    match_id: Any,
    *,
    from_status: str,
    to_status: str,
    validated_at=None,
) -> Mapping[str, Any] | None:
    """Atomic CAS: move the match from `from_status` to `to_status` only if
    the doc is still in the expected state. Returns the doc after the
    update, or None if the transition did not happen (concurrent: another
    vote has already validated).

    Set `validated_at` if the target is `validated_a` or `validated_b`. The
    `validated_at` parameter allows overriding the value (used by the
    self-repair of `check_vote_timeouts` to reference the moment when the
    majority was actually reached rather than `now`)."""
    from datetime import datetime

    update: dict[str, Any] = {"status": to_status}
    if to_status in ("validated_a", "validated_b"):
        update["validated_at"] = validated_at or datetime.now(UTC)
    return get_matches_col(db).find_one_and_update(
        {"_id": match_id, "status": from_status},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )


def claim_match_for_elo(
    db: Database,
    match_id: Any,
) -> Mapping[str, Any] | None:
    """Atomic claim: mark `elo_applied=True` only if not already applied.

    Prevents double-application of ELO if the HenrikDev verification
    retries after a crash between `apply_match_validation` and
    `set_match_henrik_verified`.

    Returns:
        The doc after the claim if we obtained the lock, None if already claimed.
    """
    from datetime import datetime

    return get_matches_col(db).find_one_and_update(
        {
            "_id": match_id,
            "status": {"$in": ["validated_a", "validated_b"]},
            "elo_applied": {"$ne": True},
        },
        {
            "$set": {
                "elo_applied": True,
                "elo_applied_at": datetime.now(UTC),
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def release_elo_claim(
    db: Database,
    match_id: Any,
) -> None:
    """Cancel the claim if the ELO application failed (rollback)."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$unset": {"elo_applied": "", "elo_applied_at": ""}},
    )


def mark_match_cleanup_started(db: Database, match_id: Any) -> None:
    """Set `delete_started_at` on the match doc just before the call to
    `delete_match_category`. Acts as a safety net: if the bot crashes
    between the start of the deletion (3 out of 4 channels deleted for
    example) and the terminal status transition, startup can detect the
    interrupted cleanup and resume it (see `find_match_ids_with_cleanup_started`)."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": {"delete_started_at": datetime.now(UTC)}},
    )


def find_category_ids_with_cleanup_started(db: Database, *, origin_guild_id: int | str) -> set[int]:
    """Returns the category_ids of matches where a cleanup was started
    (delete_started_at set). Used at boot to exclude these categories
    from the `active_ids` set: even if their status is still "active", we
    know we tried to delete them -> the orphan cleanup resumes
    idempotently. Without this signal, a cleanup interrupted between
    the Discord call and the status update would leave the orphan
    category visible but protected until the next admin
    /match-cleanup."""
    gid = _to_int_id(origin_guild_id, field="origin_guild_id")
    cursor = get_matches_col(db).find(
        {
            "origin_guild_id": gid,
            "delete_started_at": {"$exists": True},
            "category_id": {"$ne": None},
        },
        {"category_id": 1},
    )
    return {int(m["category_id"]) for m in cursor if m.get("category_id")}


def find_validated_unverified(
    db: Database,
    cutoff_dt,
    *,
    origin_guild_id: int | None = None,
) -> list[Mapping[str, Any]]:
    """Matches validated_a/b with validated_at <= cutoff_dt, not yet Henrik
    verified AND without ELO already applied (elo_applied != True).

    The filter on `elo_applied` prevents the next tick from reprocessing a
    match whose ELO was already applied but whose `henrik_verified` was
    not written (crash between the two operations).

    If `origin_guild_id` is provided, the scan is limited to matches of
    that guild (multi-guild scoping). Otherwise, scans all guilds (compat
    tests / single-guild deployment)."""
    filt: dict[str, Any] = {
        "status": {"$in": ["validated_a", "validated_b"]},
        "validated_at": {"$lte": cutoff_dt},
        "elo_applied": {"$ne": True},
        "$or": [
            {"henrik_verified": {"$exists": False}},
            {"henrik_verified": False},
        ],
    }
    if origin_guild_id is not None:
        filt["origin_guild_id"] = _to_int_id(origin_guild_id, field="origin_guild_id")
    return list(get_matches_col(db).find(filt))


def set_match_henrik_verified(
    db: Database,
    match_id: Any,
    *,
    found: bool,
    multipliers: dict[str, float] | None = None,
) -> None:
    update: dict[str, Any] = {
        "henrik_verified": True,
        "henrik_found": bool(found),
    }
    if multipliers is not None:
        update["henrik_multipliers"] = {str(k): float(v) for k, v in multipliers.items()}
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": update},
    )


def set_match_elo_results(
    db: Database,
    match_id: Any,
    results: Mapping[str, Any],
) -> None:
    """Persist the per-player ELO deltas for a validated match.

    `results` maps user_id -> {delta, old, new, win} (see
    `elo_updater.build_elo_results`). Idempotent ``$set``: re-running a
    verification overwrites with identical values. Present only on matches
    validated after this feature shipped (older matches have no field)."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": {"elo_results": dict(results)}},
    )


def set_match_score(
    db: Database,
    match_id: Any,
    score_a: int,
    score_b: int,
) -> None:
    """Persist the final round score (team_a vs team_b) for a match.

    Only available when HenrikDev returned the custom (pro queue); other
    queues / un-found matches keep no score. Idempotent ``$set``."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": {"score_a": int(score_a), "score_b": int(score_b)}},
    )


def set_match_rounds(
    db: Database,
    match_id: Any,
    rounds: list[Mapping[str, Any]],
) -> None:
    """Persist the per-round breakdown for a match (website round bar).

    Each entry is ``{"winner": "a"|"b"|"", "end": str}`` (see
    `match_verifier.compute_round_breakdown`). Henrik-only; idempotent
    ``$set``."""
    get_matches_col(db).update_one(
        {"_id": match_id},
        {"$set": {"rounds": [dict(r) for r in rounds]}},
    )


def get_match_player_stats_col(db: Database) -> Collection:
    """Match player stats collection shared across all guilds.

    Stores per-match, per-player stats including damage, kills, deaths,
    rating 2.0, etc. _id is compound: match_id:user_id."""
    col = db["match_player_stats"]
    _ensure_indexes(col, "match_player_stats")
    return col


def insert_match_player_stats(db: Database, docs: list[Mapping[str, Any]]) -> int:
    """Bulk insert per-match player stat docs. Returns inserted count.

    Idempotent: duplicate _id errors are swallowed (the caller relies
    on this when the Henrik verifier retries the same match)."""
    if not docs:
        return 0
    from pymongo.errors import BulkWriteError

    col = get_match_player_stats_col(db)
    try:
        res = col.insert_many(list(docs), ordered=False)
        return len(res.inserted_ids)
    except BulkWriteError as e:
        details = getattr(e, "details", {}) or {}
        return int(details.get("nInserted", 0))


def get_player_rating_aggregates_col(db: Database) -> Collection:
    col = db["player_rating_aggregates"]
    _ensure_indexes(col, "player_rating_aggregates")
    return col


_AGGREGATE_COUNTER_FIELDS: tuple[str, ...] = (
    "games",
    "rounds_played",
    "kills",
    "deaths",
    "assists",
    "damage_made",
    "damage_received",
    "headshots",
    "bodyshots",
    "legshots",
    "multikills_2k",
    "multikills_3k",
    "multikills_4k",
    "multikills_5k",
    "first_kills",
    "first_deaths",
    "kast_rounds",
    "rating_2_0_sum",
    "acs_sum",
    "acs_games",
)


def update_rating_aggregates(db: Database, deltas: list[Mapping[str, Any]]) -> None:
    """For each delta, upsert the {user_id}:{queue_type} aggregate
    doc, incrementing the counter fields by the delta and stamping
    updated_at = now.
    """
    if not deltas:
        return
    col = get_player_rating_aggregates_col(db)
    now = datetime.now(UTC)
    for d in deltas:
        user_id = str(d["user_id"])
        queue_type = str(d["queue_type"])
        _check_queue_type(queue_type)
        inc = {f: d.get(f, 0) for f in _AGGREGATE_COUNTER_FIELDS}
        col.update_one(
            {"_id": f"{user_id}:{queue_type}"},
            {
                "$inc": inc,
                "$set": {
                    "user_id": user_id,
                    "queue_type": queue_type,
                    "updated_at": now,
                },
            },
            upsert=True,
        )


def get_rating_aggregate(
    db: Database, *, user_id: int | str, queue_type: str
) -> Mapping[str, Any] | None:
    return get_player_rating_aggregates_col(db).find_one({"_id": f"{user_id}:{queue_type}"})


def get_leaderboard_state_col(db: Database, guild_id: int | str) -> Collection:
    """Stores the auto-refresh leaderboard state (1 doc per guild).

    Allows the refresh to find its previous message via a persisted
    `message_id` rather than scanning `chan.history(limit=20)`, which
    misses older leaderboards if someone has spammed >= 20 messages since."""
    return db[f"leaderboard_state_{guild_id}"]


def get_leaderboard_message_id(
    db: Database,
    guild_id: int | str,
    queue_type: str,
) -> int | None:
    _check_queue_type(queue_type)
    doc = get_leaderboard_state_col(db, guild_id).find_one(
        {"_id": leaderboard_state_id(queue_type)}
    )
    if not doc:
        return None
    mid = doc.get("message_id")
    return int(mid) if mid is not None else None


def set_leaderboard_message_id(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    message_id: int,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).update_one(
        {"_id": leaderboard_state_id(queue_type)},
        {"$set": {"message_id": _to_int_id(message_id, field="message_id")}},
        upsert=True,
    )


def clear_leaderboard_message_id(
    db: Database,
    guild_id: int | str,
    queue_type: str,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).delete_one({"_id": leaderboard_state_id(queue_type)})


# -- Weekly Pro Queue leaderboard ----------------------------------
# Mirror ELO collection for the Pro Queue, wiped every Monday 00:00
# Europe/Paris. Same fields as the `elo` collection but usage limited
# to the Pro Queue (queue_type always "pro").
WEEKLY_PRO_QUEUE_TYPE: str = "pro"


def get_applications_col(db: Database, guild_id: int | str) -> Collection:
    """1 collection per guild for applications (state machine)."""
    return db[f"applications_{guild_id}"]


def register_application(
    db: Database,
    guild_id: int | str,
    message_id: int | str,
    applicant_id: int | str,
    *,
    is_staff: bool = False,
) -> None:
    """Record an application in the `pending` state. `_id` is the Discord
    message (carrying the accept/refuse buttons). Idempotent via $setOnInsert."""
    from datetime import datetime

    get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id)},
        {
            "$setOnInsert": {
                "applicant_id": str(applicant_id),
                "is_staff": bool(is_staff),
                "status": "pending",
                "created_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )


def claim_application_decision(
    db: Database,
    guild_id: int | str,
    message_id: int | str,
    *,
    status: str,
    decided_by: int | str,
) -> bool:
    """Atomic CAS: transition the application from `pending` to
    `accepted` or `refused`. Returns True if we got the decision,
    False if another admin has already decided (prevents double-handling:
    concurrent role grant + kick, double DM, etc.)."""
    from datetime import datetime

    if status not in ("accepted", "refused"):
        raise ValueError(f"invalid status: {status}")
    res = get_applications_col(db, guild_id).update_one(
        {"_id": str(message_id), "status": "pending"},
        {
            "$set": {
                "status": status,
                "decided_by": str(decided_by),
                "decided_at": datetime.now(UTC),
            }
        },
    )
    return res.modified_count == 1


def cancel_match_atomically(
    db: Database,
    *,
    channel_id: int | str,
) -> Mapping[str, Any] | None:
    """Atomic CAS: cancel the match of channel `channel_id` if and only if
    its status is still pending/validated/contested and the ELO has not
    yet been applied. Otherwise returns None.

    Prevents the race between `match-cancel` and:
      - a concurrent vote that would validate the match (find_one would see
        `pending` then update_one would overwrite the already transitioned
        `validated_a`)
      - `_verify_match` which would apply the ELO (status=cancelled but
        elo_applied=True: inconsistent state)."""
    return get_matches_col(db).find_one_and_update(
        {
            "channel_id": channel_id,
            "status": {
                "$in": [
                    "preparing",
                    "pending",
                    "validated_a",
                    "validated_b",
                    "contested",
                ]
            },
            "elo_applied": {"$ne": True},
        },
        {"$set": {"status": "cancelled"}},
        return_document=ReturnDocument.BEFORE,
    )


def force_match_result_atomically(
    db: Database,
    *,
    channel_id: int | str,
    winner: str,
) -> Mapping[str, Any] | None:
    """Atomic CAS: force the winner of the match in `channel_id`.

    Transitions a `pending` or `contested` match (whose ELO has not yet
    been applied) to `validated_a`/`validated_b`, stamping `validated_at`
    with the current time. Returns the doc AFTER the update, or None if no
    eligible match exists (already validated/cancelled/cleaned_up, ELO
    already applied, or no match in this channel).

    Used by /match-force-result so an admin can settle a vote that timed
    out (`contested`) — or pre-empt a still-open `pending` vote — without
    waiting for the 7/10 majority. The downstream HenrikDev verification +
    ELO application then run exactly as for a normal vote validation.

    The CAS on `status`/`elo_applied` mirrors `cancel_match_atomically`:
    if a concurrent vote validates the match or `_verify_match` claims the
    ELO between read and write, the force fails cleanly instead of
    overwriting an already-settled result."""
    if winner not in ("a", "b"):
        raise ValueError(f"winner must be 'a' or 'b', received {winner!r}")
    to_status = "validated_a" if winner == "a" else "validated_b"
    return get_matches_col(db).find_one_and_update(
        {
            "channel_id": channel_id,
            "status": {"$in": ["pending", "contested"]},
            "elo_applied": {"$ne": True},
        },
        {"$set": {"status": to_status, "validated_at": datetime.now(UTC)}},
        return_document=ReturnDocument.AFTER,
    )


# -- Warns (moderation) --------------------------------------------
# Per-guild storage: every server has its own warn history.


def get_warns_col(db: Database, guild_id: int | str) -> Collection:
    return db[f"warns_{guild_id}"]


def add_warn(
    db: Database,
    guild_id: int | str,
    *,
    member_id: int,
    member_name: str,
    moderator_id: int,
    moderator_name: str,
    reason: str,
) -> None:
    from datetime import datetime

    get_warns_col(db, guild_id).insert_one(
        {
            "member_id": int(member_id),
            "member_name": member_name,
            "moderator_id": int(moderator_id),
            "moderator_name": moderator_name,
            "reason": reason,
            "timestamp": datetime.now(UTC),
        }
    )


def list_warns(
    db: Database,
    guild_id: int | str,
    *,
    member_id: int | None = None,
    limit: int = 50,
) -> list[Mapping[str, Any]]:
    """Returns the guild's warns, most recent first.

    If `member_id` is provided, filters on the warns of that member only.
    """
    filt: dict[str, Any] = {}
    if member_id is not None:
        filt["member_id"] = int(member_id)
    cursor = get_warns_col(db, guild_id).find(filt).sort("timestamp", -1).limit(limit)
    return list(cursor)


def reserve_match_number(db: Database, *, guild_id: int) -> int:
    """Atomically increment guild_state.match_counter and return the new value.

    Uses $inc with upsert so the counter survives the very first call.
    """
    doc = db["guild_state"].find_one_and_update(
        {"_id": guild_id},
        {"$inc": {"match_counter": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    # upsert=True + ReturnDocument.AFTER guarantees a non-None doc.
    # Explicit check (vs `assert`) so the guarantee survives `python -O`.
    if doc is None:
        raise RuntimeError(
            "find_one_and_update(upsert=True, AFTER) returned None — pymongo contract changed"
        )
    return int(doc["match_counter"])
