"""
Unit tests of the pagination logic (no Discord, no MongoDB).

These tests validate ONLY the page count computation and the navigation
bounds. They cannot detect a bug in the Discord API.

Usage:
    pip install pytest
    pytest test_pagination.py -v
"""

import pytest

PAGE_SIZE = 15


def total_pages(n_players: int) -> int:
    """Reproduces the bot's computation (line 215 of the original file)."""
    return max(1, (n_players + PAGE_SIZE - 1) // PAGE_SIZE)


def is_prev_disabled(page: int) -> bool:
    return page == 0


def is_next_disabled(page: int, total: int) -> bool:
    return page >= total - 1


def clamp_page(new_page: int, total: int) -> int | None:
    """Return None if the click must be ignored (out of bounds)."""
    if new_page < 0 or new_page >= total:
        return None
    return new_page


# -- Tests: page count --
@pytest.mark.parametrize(
    "n,expected",
    [
        (0, 1),  # empty list -> 1 page (default)
        (1, 1),  # 1 player -> 1 page
        (15, 1),  # exactly PAGE_SIZE -> 1 page
        (16, 2),  # one more -> 2 pages
        (29, 2),
        (30, 2),
        (31, 3),
        (100, 7),
        (150, 10),
    ],
)
def test_total_pages(n, expected):
    assert total_pages(n) == expected


# -- Tests: button state --
def test_prev_disabled_on_first_page():
    assert is_prev_disabled(0) is True


def test_prev_enabled_on_other_pages():
    assert is_prev_disabled(1) is False
    assert is_prev_disabled(5) is False


def test_next_disabled_on_last_page():
    assert is_next_disabled(1, total=2) is True
    assert is_next_disabled(6, total=7) is True


def test_next_enabled_when_more_pages():
    assert is_next_disabled(0, total=2) is False
    assert is_next_disabled(3, total=7) is False


def test_both_disabled_when_only_one_page():
    assert is_prev_disabled(0) is True
    assert is_next_disabled(0, total=1) is True


# -- Tests: navigation (clamp) --
def test_clamp_valid_page():
    assert clamp_page(2, total=5) == 2


def test_clamp_first_page():
    assert clamp_page(0, total=5) == 0


def test_clamp_last_page():
    assert clamp_page(4, total=5) == 4


def test_clamp_below_zero_rejected():
    assert clamp_page(-1, total=5) is None


def test_clamp_above_total_rejected():
    assert clamp_page(5, total=5) is None
    assert clamp_page(99, total=5) is None


# -- Tests: chunk splitting --
def test_chunk_first_page():
    players = list(range(30))
    page = 0
    chunk = players[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    assert chunk == list(range(15))


def test_chunk_second_page():
    players = list(range(30))
    page = 1
    chunk = players[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    assert chunk == list(range(15, 30))


def test_chunk_partial_last_page():
    """Case where the last page is not full - WARNING, may break
    generate_leaderboard if it is not tolerant to variable-sized chunks."""
    players = list(range(16))
    chunk_p2 = players[15:30]
    assert len(chunk_p2) == 1, "Page 2 contains only 1 player here"


def test_chunk_empty_when_out_of_bounds():
    players = list(range(15))
    chunk = players[15:30]
    assert chunk == []


from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import mongomock


@pytest.mark.asyncio
async def test_build_leaderboard_payload_filters_by_queue_type():
    from services.leaderboard_refresh import build_leaderboard_payload
    from services.repository import get_elo_col, player_doc_id

    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db)
    col.insert_many(
        [
            {
                "_id": player_doc_id(1, "open"),
                "user_id": "1",
                "name": "A",
                "elo": 2500,
                "wins": 5,
                "losses": 1,
                "queue_type": "open",
                "last_played": datetime.now(UTC),
            },
            {
                "_id": player_doc_id(2, "open"),
                "user_id": "2",
                "name": "B",
                "elo": 1500,
                "wins": 1,
                "losses": 5,
                "queue_type": "open",
                "last_played": datetime.now(UTC),
            },
        ]
    )

    guild = MagicMock()
    guild.id = 42
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "A"
    fake_member.display_avatar.replace.return_value.url = "http://av/1.png"
    guild.get_member.return_value = fake_member

    file_pro, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    file_semipro, _ = await build_leaderboard_payload(guild, db, queue_type="advanced")
    file_open, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    file_gc, _ = await build_leaderboard_payload(guild, db, queue_type="advanced")

    assert file_pro is not None
    assert file_open is not None
    assert file_semipro is None  # 0 players in Semi Pro
    assert file_gc is None  # 0 players in GC


@pytest.mark.asyncio
async def test_build_leaderboard_payload_hides_inactive_players():
    """A player inactive > 7 days (or never played) is excluded; if all
    players in a queue are inactive, the leaderboard is empty (None)."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import build_leaderboard_payload
    from services.repository import get_elo_col, player_doc_id

    leaderboard_refresh._clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db)
    now = datetime.now(UTC)
    col.insert_many(
        [
            {  # active: played 2 days ago
                "_id": player_doc_id(1, "open"),
                "user_id": "1",
                "name": "Active",
                "elo": 2300,
                "wins": 4,
                "losses": 1,
                "queue_type": "open",
                "last_played": now - timedelta(days=2),
            },
            {  # inactive: played 8 days ago
                "_id": player_doc_id(2, "open"),
                "user_id": "2",
                "name": "Stale",
                "elo": 2600,
                "wins": 9,
                "losses": 0,
                "queue_type": "open",
                "last_played": now - timedelta(days=8),
            },
            {  # never played: no last_played
                "_id": player_doc_id(3, "open"),
                "user_id": "3",
                "name": "Newbie",
                "elo": 2000,
                "wins": 0,
                "losses": 0,
                "queue_type": "open",
            },
        ]
    )

    captured: list[list[dict]] = []
    real_gen = leaderboard_refresh.generate_leaderboard

    def spy_gen(players, **kwargs):
        captured.append(list(players))
        return real_gen(players, **kwargs)

    guild = MagicMock()
    guild.id = 7
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "X"
    fake_member.display_avatar.replace.return_value.url = "http://av/x.png"
    guild.get_member.return_value = fake_member

    leaderboard_refresh.generate_leaderboard = spy_gen
    try:
        file_pro, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    finally:
        leaderboard_refresh.generate_leaderboard = real_gen

    assert file_pro is not None
    # Only the active player is rendered (Stale @8d and Newbie/never excluded).
    assert len(captured[0]) == 1
    assert captured[0][0]["elo"] == 2300  # the active player's ELO


# -- Rendered page cache --
#
# Tests of the lazy cache added in services/leaderboard_refresh.py.
# Every test starts by emptying the global (process-wide) cache to
# guarantee isolation between cases.


def _make_guild_with_member(guild_id: int = 99):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "P"
    fake_member.display_avatar.replace.return_value.url = "http://av/p.png"
    guild.get_member.return_value = fake_member
    return guild


def _seed_pro_open(db, guild_id: int, n: int = 1) -> None:
    from services.repository import get_elo_col, player_doc_id

    col = get_elo_col(db)
    docs = []
    # User IDs offset per guild to avoid collisions in the shared `elo`
    # collection (post-refactor shared collections).
    offset = guild_id * 1000
    for i in range(1, n + 1):
        uid = offset + i
        docs.append(
            {
                "_id": player_doc_id(uid, "open"),
                "user_id": str(uid),
                "name": f"P{uid}",
                "elo": 2500 + i,
                "wins": 5,
                "losses": 0,
                "queue_type": "open",
                "last_played": datetime.now(UTC),
            }
        )
    col.insert_many(docs)


@pytest.mark.asyncio
async def test_page_cache_hit_skips_render():
    """A 2nd call on the same page must not re-render via PIL."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import build_leaderboard_payload

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    guild = _make_guild_with_member(99)

    call_count = {"n": 0}
    real_gen = leaderboard_refresh.generate_leaderboard

    def counting_gen(*a, **kw):
        call_count["n"] += 1
        return real_gen(*a, **kw)

    leaderboard_refresh.generate_leaderboard = counting_gen
    try:
        file1, _ = await build_leaderboard_payload(guild, db, queue_type="open")
        file2, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    finally:
        leaderboard_refresh.generate_leaderboard = real_gen

    assert file1 is not None and file2 is not None
    assert call_count["n"] == 1, (
        f"generate_leaderboard called {call_count['n']}x instead of 1 "
        "(cache hit expected on 2nd call)"
    )


@pytest.mark.asyncio
async def test_page_cache_returns_fresh_bytesio_per_call():
    """Cache stores bytes, not a BytesIO. Every hit must produce a
    readable discord.File (fresh internal BytesIO)."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import build_leaderboard_payload

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    guild = _make_guild_with_member(99)

    file1, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    file2, _ = await build_leaderboard_payload(guild, db, queue_type="open")

    # The two discord.File objects must be distinct (fresh BytesIO) and
    # contain readable bytes -> otherwise the 2nd Discord send fails.
    bytes1 = file1.fp.read()
    bytes2 = file2.fp.read()
    assert bytes1 == bytes2
    assert len(bytes1) > 0


@pytest.mark.asyncio
async def test_page_cache_invalidation_clears_queue_entries():
    """_cache_invalidate(g, qt) must clear ALL entries for that
    (guild, queue_type), whatever the page."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _PAGE_CACHE,
        _cache_invalidate,
        build_leaderboard_payload,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99, n=20)  # > 15 -> 2 pages
    guild = _make_guild_with_member(99)

    await build_leaderboard_payload(guild, db, queue_type="open", page=0)
    await build_leaderboard_payload(guild, db, queue_type="open", page=1)
    assert (99, "open", 0) in _PAGE_CACHE
    assert (99, "open", 1) in _PAGE_CACHE

    removed = _cache_invalidate(99, "open")
    assert removed == 2
    assert (99, "open", 0) not in _PAGE_CACHE
    assert (99, "open", 1) not in _PAGE_CACHE


@pytest.mark.asyncio
async def test_page_cache_invalidation_is_per_queue():
    """Invalidating Open must NOT affect Advanced."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _PAGE_CACHE,
        _cache_invalidate,
        build_leaderboard_payload,
    )
    from services.repository import get_elo_col, player_doc_id

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    # Seed an Advanced-queue player so the advanced leaderboard renders too.
    get_elo_col(db).insert_one(
        {
            "_id": player_doc_id(99001, "advanced"),
            "user_id": "99001",
            "name": "Adv",
            "elo": 2500,
            "wins": 5,
            "losses": 0,
            "queue_type": "advanced",
            "last_played": datetime.now(UTC),
        }
    )
    guild = _make_guild_with_member(99)

    await build_leaderboard_payload(guild, db, queue_type="open")
    await build_leaderboard_payload(guild, db, queue_type="advanced")
    assert (99, "open", 0) in _PAGE_CACHE
    assert (99, "advanced", 0) in _PAGE_CACHE

    removed = _cache_invalidate(99, "open")
    assert removed == 1
    assert (99, "open", 0) not in _PAGE_CACHE
    assert (99, "advanced", 0) in _PAGE_CACHE, "Advanced must NOT be invalidated"


@pytest.mark.asyncio
async def test_page_cache_invalidation_is_per_guild():
    """Invalidating guild 99 must NOT affect guild 100."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _PAGE_CACHE,
        _cache_invalidate,
        build_leaderboard_payload,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    db = mongomock.MongoClient(tz_aware=True).db
    _seed_pro_open(db, 99)
    _seed_pro_open(db, 100)

    await build_leaderboard_payload(_make_guild_with_member(99), db, queue_type="open")
    await build_leaderboard_payload(_make_guild_with_member(100), db, queue_type="open")
    assert (99, "open", 0) in _PAGE_CACHE
    assert (100, "open", 0) in _PAGE_CACHE

    _cache_invalidate(99, "open")
    assert (99, "open", 0) not in _PAGE_CACHE
    assert (100, "open", 0) in _PAGE_CACHE, "Another guild's cache must not be touched"


def test_page_cache_lru_eviction():
    """Beyond _PAGE_CACHE_MAXSIZE, the oldest (FIFO) entry is evicted."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _PAGE_CACHE,
        _PAGE_CACHE_MAXSIZE,
        _cache_set,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    # Fill up to the limit: (guild_id varies to generate unique keys)
    for g in range(_PAGE_CACHE_MAXSIZE):
        _cache_set(g, "open", 0, b"x", 1)
    assert len(_PAGE_CACHE) == _PAGE_CACHE_MAXSIZE
    assert (0, "open", 0) in _PAGE_CACHE

    # Add one more entry -> the oldest (guild=0) is evicted
    _cache_set(_PAGE_CACHE_MAXSIZE, "open", 0, b"x", 1)
    assert len(_PAGE_CACHE) == _PAGE_CACHE_MAXSIZE
    assert (0, "open", 0) not in _PAGE_CACHE
    assert (_PAGE_CACHE_MAXSIZE, "open", 0) in _PAGE_CACHE


def test_page_cache_get_promotes_lru_order():
    """A cache hit must promote the key to most-recent (anti-eviction)."""
    from services import leaderboard_refresh
    from services.leaderboard_refresh import (
        _PAGE_CACHE,
        _cache_get,
        _cache_set,
    )

    leaderboard_refresh._clear_page_cache_for_tests()

    # Fill half the limit with distinct keys
    for g in range(3):
        _cache_set(g, "open", 0, b"x", 1)

    # Hit on the oldest (g=0) -> it becomes the most-recent
    assert _cache_get(0, "open", 0) is not None

    # Verify it is at the end (most-recent) of the OrderedDict
    last_key = next(reversed(_PAGE_CACHE))
    assert last_key == (0, "open", 0)


def test_find_leaderboard_channel_returns_lb_channel():
    """Returns the #leaderboard channel."""
    from services.leaderboard_refresh import _find_leaderboard_channel

    lb = MagicMock()
    lb.name = "🏆・leaderboard"
    guild = MagicMock()
    guild.text_channels = [lb]

    assert _find_leaderboard_channel(guild) is lb


def test_find_leaderboard_channel_returns_none_if_missing():
    from services.leaderboard_refresh import _find_leaderboard_channel

    other = MagicMock()
    other.name = "general"
    guild = MagicMock()
    guild.text_channels = [other]

    assert _find_leaderboard_channel(guild) is None
