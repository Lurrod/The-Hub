"""Tests for `MatchCog._fetch_henrik_match_summary`, the single gate in
front of the scoreboard, the Rating 2.0 and the extended stats.

Regression context: the lookup used to be keyed on the `riot_name` /
`riot_tag` frozen at `/link-riot` time. A player renaming their Riot ID
made HenrikDev answer HTTP 404 for good, the error was swallowed without
a log, and no scoreboard was ever posted again.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from cogs.match import MatchCog
from services import repository

LEADER_ID = 0


def _seed_match(db, guild_id: int = 42):
    return repository.create_match(
        db,
        origin_guild_id=guild_id,
        team_a=[{"id": i, "name": f"P{i}", "elo": 2000} for i in range(0, 5)],
        team_b=[{"id": i, "name": f"P{i}", "elo": 2000} for i in range(5, 10)],
        map_name="Ascent",
        lobby_leader_id=LEADER_ID,
        category_name="Match #1",
        message_id=555,
        channel_id=100,
        queue_type="open",
    )


def _seed_riot_accounts(db, *, count: int = 10) -> None:
    for i in range(count):
        repository.link_riot_account(
            db,
            i,
            riot_name=f"Player{i}",
            riot_tag="EUW",
            riot_region="eu",
            puuid=f"puuid-{i}",
            peak_elo=2000,
            source="peak_6mo",
        )


def _cog(db):
    import bot as bot_module

    cog = MatchCog(bot_module.bot, db, henrik_client=MagicMock())
    cog.bot = MagicMock()
    return cog


async def test_fetch_summary_queries_by_puuid_with_leader_first(monkeypatch):
    """The history must be requested by puuid (immutable), starting with
    the lobby leader, with the other players as fallback."""
    import bot as bot_module
    from cogs.match import _verification

    match_id = _seed_match(bot_module.db)
    _seed_riot_accounts(bot_module.db)

    captured: dict = {}

    def _fake_find(client, **kwargs):
        captured.update(kwargs)
        return MagicMock(name="summary")

    monkeypatch.setattr(_verification, "find_henrik_custom_match", _fake_find)

    cog = _cog(bot_module.db)
    match_doc = repository.get_match(bot_module.db, match_id)
    result = await cog._fetch_henrik_match_summary(MagicMock(), match_doc)

    assert result is not None
    summary, team_a_uid_by_puuid, team_b_uid_by_puuid = result
    assert set(team_a_uid_by_puuid.values()) == {str(i) for i in range(5)}
    assert set(team_b_uid_by_puuid.values()) == {str(i) for i in range(5, 10)}

    assert captured["region"] == "eu"
    assert captured["expected_puuids"] == {f"puuid-{i}" for i in range(10)}
    lookup = captured["lookup_puuids"]
    assert lookup[0] == f"puuid-{LEADER_ID}", "the lobby leader must be queried first"
    assert len(lookup) == 3, "fallback capped at MAX_HISTORY_LOOKUPS players"
    assert len(set(lookup)) == 3, "no duplicated puuid in the fallback list"


async def test_fetch_summary_falls_back_to_a_player_when_leader_unlinked(monkeypatch):
    """A leader without a Riot account no longer aborts the lookup: any
    other player of the lobby saw the same custom."""
    import bot as bot_module
    from cogs.match import _verification

    match_id = _seed_match(bot_module.db)
    _seed_riot_accounts(bot_module.db, count=11)
    # The leader keeps a linked account (queue gate) but loses its puuid.
    repository.get_riot_col(bot_module.db).update_one(
        {"_id": str(LEADER_ID)},
        {"$set": {"puuid": ""}},
    )
    # A replacement player keeps the roster at 10 linked puuids.
    repository.get_matches_col(bot_module.db).update_one(
        {"_id": match_id},
        {"$set": {"team_a.0": {"id": 10, "name": "P10", "elo": 2000}}},
    )

    captured: dict = {}
    monkeypatch.setattr(
        _verification,
        "find_henrik_custom_match",
        lambda client, **kwargs: (captured.update(kwargs), MagicMock())[1],
    )

    cog = _cog(bot_module.db)
    match_doc = repository.get_match(bot_module.db, match_id)
    result = await cog._fetch_henrik_match_summary(MagicMock(), match_doc)

    assert result is not None
    assert captured["lookup_puuids"], "at least one player puuid must be tried"
    assert "" not in captured["lookup_puuids"]


async def test_fetch_summary_returns_none_when_a_player_is_not_linked(monkeypatch, caplog):
    """Fewer than 10 linked puuids is unrecoverable — but it must be
    logged, not silently swallowed."""
    import bot as bot_module
    from cogs.match import _verification

    match_id = _seed_match(bot_module.db)
    _seed_riot_accounts(bot_module.db, count=9)  # player 9 never ran /link-riot

    called = MagicMock()
    monkeypatch.setattr(_verification, "find_henrik_custom_match", called)

    cog = _cog(bot_module.db)
    match_doc = repository.get_match(bot_module.db, match_id)
    with caplog.at_level("WARNING"):
        result = await cog._fetch_henrik_match_summary(MagicMock(), match_doc)

    assert result is None
    called.assert_not_called()
    assert "9/10" in caplog.text
    assert "'9'" in caplog.text, "the unlinked player must be named in the log"


async def test_fetch_summary_uses_created_at_as_cutoff(monkeypatch):
    """The custom is searched after the bot created the match, so an
    earlier custom of the same group is never picked up."""
    import bot as bot_module
    from cogs.match import _verification

    match_id = _seed_match(bot_module.db)
    _seed_riot_accounts(bot_module.db)
    created = datetime.now(UTC) - timedelta(minutes=40)
    repository.get_matches_col(bot_module.db).update_one(
        {"_id": match_id},
        {"$set": {"created_at": created}},
    )

    captured: dict = {}
    monkeypatch.setattr(
        _verification,
        "find_henrik_custom_match",
        lambda client, **kwargs: (captured.update(kwargs), MagicMock())[1],
    )

    cog = _cog(bot_module.db)
    match_doc = repository.get_match(bot_module.db, match_id)
    await cog._fetch_henrik_match_summary(MagicMock(), match_doc)

    # Mongo truncates to the millisecond, hence the tolerance.
    assert abs(captured["after"] - created) < timedelta(seconds=1)
