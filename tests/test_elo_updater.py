"""Tests for the post-vote ELO update (flat ±20, all queues)."""

import pytest

from services import elo_calc, repository
from services.elo_updater import (
    FLAT_ELO_CHANGE,
    apply_match_validation,
)


# ── compute_match_elo_change (pure formula, zero-sum) ─────────────
@pytest.mark.parametrize("avg", [0, 300, 2100, 2400, 2700, 3000, 5000])
def test_compute_match_elo_change(avg):
    """Strict zero-sum: gain == loss == ELO_BASE_CHANGE whatever the avg."""
    g, l = elo_calc.compute_match_elo_change(avg)
    assert g == elo_calc.ELO_BASE_CHANGE
    assert l == elo_calc.ELO_BASE_CHANGE
    assert g == l == 20


def test_compute_match_elo_change_rejects_negative():
    with pytest.raises(ValueError):
        elo_calc.compute_match_elo_change(-100)


# ── compute_team_avg_elo ──────────────────────────────────────────
def test_team_avg_empty_returns_0():
    assert elo_calc.compute_team_avg_elo([]) == 0


def test_team_avg_normal():
    players = [{"elo": 1000}, {"elo": 2000}, {"elo": 1500}]
    assert elo_calc.compute_team_avg_elo(players) == 1500


def test_team_avg_handles_missing_elo_key():
    players = [{"elo": 1500}, {"name": "no-elo"}]  # 2nd has no elo -> 0
    assert elo_calc.compute_team_avg_elo(players) == 750


# ── apply_match_validation ────────────────────────────────────────
def _make_match(status="validated_a", elo=2400):
    return {
        "team_a": [{"id": i, "name": f"A{i}", "elo": elo} for i in range(5)],
        "team_b": [{"id": 5 + i, "name": f"B{i}", "elo": elo} for i in range(5)],
        "status": status,
        "queue_type": "open",
    }


def _seed_baseline_elo(db, guild_id: int, ids: range, baseline: int) -> None:
    """Give each player a high-enough starting ELO to avoid the floor."""
    col = repository.get_elo_col(db)
    col.delete_many({})
    for i in ids:
        col.insert_one(
            {
                "_id": f"{i}:open",
                "name": f"P{i}",
                "elo": baseline,
                "wins": 0,
                "losses": 0,
                "queue_type": "open",
                "user_id": str(i),
            }
        )


def test_invalid_status_raises():
    import bot as bot_module

    with pytest.raises(ValueError):
        apply_match_validation(bot_module.db, _make_match(status="pending"))


def test_validated_a_winners_get_gain():
    import bot as bot_module

    match = _make_match(status="validated_a", elo=2400)
    outcome = apply_match_validation(bot_module.db, match)

    assert outcome.gain == FLAT_ELO_CHANGE == 20
    assert outcome.loss == FLAT_ELO_CHANGE == 20
    assert outcome.avg_elo == 2400
    assert outcome.weighted is False

    winners = [c for c in outcome.changes if c.win]
    losers = [c for c in outcome.changes if not c.win]
    assert len(winners) == 5
    assert len(losers) == 5
    assert {c.user_id for c in winners} == {"0", "1", "2", "3", "4"}
    assert {c.user_id for c in losers} == {"5", "6", "7", "8", "9"}


def test_validated_b_swaps_winners_losers():
    import bot as bot_module

    match = _make_match(status="validated_b", elo=2400)
    outcome = apply_match_validation(bot_module.db, match)

    winners_ids = {c.user_id for c in outcome.changes if c.win}
    assert winners_ids == {"5", "6", "7", "8", "9"}


def test_winners_get_plus_gain_in_db():
    import bot as bot_module

    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, match)

    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(5):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        assert doc["elo"] == 2020  # 2000 + 20
        assert doc["wins"] == 1
        assert doc["losses"] == 0


def test_apply_match_validation_stamps_last_played():
    from datetime import datetime

    import bot as bot_module

    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, match)

    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(10):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        assert isinstance(doc.get("last_played"), datetime)


def test_losers_get_minus_loss_in_db():
    import bot as bot_module

    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, match)

    elo_col = repository.get_elo_col(bot_module.db)
    for i in range(5, 10):
        doc = elo_col.find_one({"_id": f"{i}:open"})
        # New player : ELO_START=2000, 2000 - 20 = 1980
        assert doc["elo"] == 1980
        assert doc["losses"] == 1
        assert doc["wins"] == 0


def test_loser_existing_elo_decreases_correctly():
    import bot as bot_module

    elo_col = repository.get_elo_col(bot_module.db)
    elo_col.insert_one(
        {
            "_id": "5:open",
            "name": "B0",
            "elo": 50,
            "wins": 0,
            "losses": 0,
            "queue_type": "open",
            "user_id": "5",
        }
    )

    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, match)

    doc = elo_col.find_one({"_id": "5:open"})
    assert doc["elo"] == 30  # 50 - 20
    assert doc["losses"] == 1


def test_loser_floored_at_zero():
    import bot as bot_module

    elo_col = repository.get_elo_col(bot_module.db)
    elo_col.insert_one(
        {
            "_id": "5:open",
            "name": "B0",
            "elo": 5,
            "wins": 0,
            "losses": 0,
            "queue_type": "open",
            "user_id": "5",
        }
    )

    match = _make_match(elo=2400)  # loss=20 but current=5 -> 0
    apply_match_validation(bot_module.db, match)

    doc = elo_col.find_one({"_id": "5:open"})
    assert doc["elo"] == 0


def test_multipliers_arg_is_ignored():
    """Passing multipliers no longer scales individual deltas; everyone
    gets the flat ±20."""
    import bot as bot_module

    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=3000)
    multipliers = {"0": 1.5, "1": 0.5, "5": 1.5, "6": 0.5}
    outcome = apply_match_validation(bot_module.db, match, multipliers=multipliers)
    assert outcome.gain == 20
    assert outcome.loss == 20
    assert outcome.weighted is False
    for c in outcome.changes:
        assert abs(c.delta) == 20


def test_flat_change_regardless_of_avg():
    """Whatever the match avg ELO, base change is always 20."""
    import bot as bot_module

    for avg in (300, 2400, 3000):
        match = _make_match(elo=avg)
        outcome = apply_match_validation(bot_module.db, match)
        assert outcome.gain == 20, f"avg={avg}, gain={outcome.gain}"
        assert outcome.loss == 20, f"avg={avg}, loss={outcome.loss}"


def test_existing_winner_keeps_history_and_adds_gain():
    import bot as bot_module

    elo_col = repository.get_elo_col(bot_module.db)
    elo_col.insert_one(
        {
            "_id": "0:open",
            "name": "A0",
            "elo": 200,
            "wins": 5,
            "losses": 3,
            "queue_type": "open",
            "user_id": "0",
        }
    )
    for i in range(1, 5):
        elo_col.insert_one(
            {
                "_id": f"{i}:open",
                "name": f"A{i}",
                "elo": 2000,
                "wins": 0,
                "losses": 0,
                "queue_type": "open",
                "user_id": str(i),
            }
        )
    for i in range(5, 10):
        elo_col.insert_one(
            {
                "_id": f"{i}:open",
                "name": f"B{i - 5}",
                "elo": 2000,
                "wins": 0,
                "losses": 0,
                "queue_type": "open",
                "user_id": str(i),
            }
        )

    match = _make_match(elo=2400)
    apply_match_validation(bot_module.db, match)

    doc = elo_col.find_one({"_id": "0:open"})
    assert doc["elo"] == 220  # 200 + 20
    assert doc["wins"] == 6
    assert doc["losses"] == 3


def test_mixed_team_avg_elo():
    """Avg is still computed over the 10 players (informational only)."""
    import bot as bot_module

    match = {
        "team_a": [{"id": i, "name": f"A{i}", "elo": 2200} for i in range(5)],
        "team_b": [{"id": 5 + i, "name": f"B{i}", "elo": 2600} for i in range(5)],
        "status": "validated_a",
        "queue_type": "open",
    }
    outcome = apply_match_validation(bot_module.db, match)
    assert outcome.avg_elo == 2400
    assert outcome.gain == 20


def test_change_dataclass_fields():
    import bot as bot_module

    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=2000)
    match = _make_match(elo=2400)
    outcome = apply_match_validation(bot_module.db, match)

    winner = next(c for c in outcome.changes if c.win)
    assert winner.delta == 20
    assert winner.old_elo == 2000
    assert winner.new_elo == 2020

    loser = next(c for c in outcome.changes if not c.win)
    assert loser.delta == -20
    assert loser.old_elo == 2000
    assert loser.new_elo == 1980


def test_zero_sum_baseline():
    """All players gain/lose the same amount -> sum(deltas) = 0."""
    import bot as bot_module

    _seed_baseline_elo(bot_module.db, 42, range(10), baseline=10000)
    match = _make_match(status="validated_a", elo=2400)
    outcome = apply_match_validation(bot_module.db, match)
    assert sum(c.delta for c in outcome.changes) == 0


def test_apply_match_validation_uses_compound_doc_id():
    """The player doc in the shared `elo` collection is created with _id=<user_id>:<queue_type>."""
    import bot as bot_module

    db = bot_module.db
    match_doc = {
        "_id": "match-advanced-1",
        "queue_type": "advanced",
        "status": "validated_a",
        "team_a": [{"id": "1", "name": "A", "elo": 2000}],
        "team_b": [{"id": "2", "name": "B", "elo": 2000}],
    }
    apply_match_validation(db, match_doc=match_doc)
    col = bot_module.db["elo"]
    assert col.find_one({"_id": "1:advanced"}) is not None
    assert col.find_one({"_id": "2:advanced"}) is not None
    assert col.find_one({"_id": "1"}) is None


# ── Pondération Rating 2.0 (open + advanced queues) ─────────────────
def _pro_match(status="validated_a"):
    return {
        "_id": "match-open-1",
        "team_a": [{"id": str(i), "name": f"A{i}", "elo": 2000} for i in range(5)],
        "team_b": [{"id": str(5 + i), "name": f"B{i}", "elo": 2000} for i in range(5)],
        "status": status,
        "queue_type": "open",
    }


def _semipro_match(status="validated_a"):
    return {
        "_id": "match-advanced-1",
        "team_a": [{"id": str(i), "name": f"A{i}", "elo": 2000} for i in range(5)],
        "team_b": [{"id": str(5 + i), "name": f"B{i}", "elo": 2000} for i in range(5)],
        "status": status,
        "queue_type": "advanced",
    }


def test_pro_queue_weighted_deltas_applied():
    import bot as bot_module

    match = _pro_match()
    # Winners 0..4, losers 5..9.
    ratings = {
        "0": 1.40,  # carry winner  -> +26
        "1": 1.00,  # avg winner    -> +20
        "5": 1.40,  # carry loser   -> -14
        "6": 0.50,  # feeding loser -> -26
    }
    outcome = apply_match_validation(bot_module.db, match, ratings=ratings)
    assert outcome.weighted is True

    col = repository.get_elo_col(bot_module.db)
    assert col.find_one({"_id": "0:open"})["elo"] == 2026  # carry win +26
    assert col.find_one({"_id": "1:open"})["elo"] == 2020  # avg win +20
    assert col.find_one({"_id": "5:open"})["elo"] == 1985  # carry loss -15 (clamped)
    assert col.find_one({"_id": "6:open"})["elo"] == 1978  # feed loss -22 (clamped)


def test_semipro_queue_weighted_deltas_applied():
    """Semi-Pro uses the same Rating 2.0 weighting as Pro (not flat ±20)."""
    import bot as bot_module

    match = _semipro_match()
    ratings = {
        "0": 1.40,  # carry winner  -> +26
        "1": 1.00,  # avg winner    -> +20
        "5": 1.40,  # carry loser   -> -15 (clamped)
        "6": 0.50,  # feeding loser -> -22 (clamped)
    }
    outcome = apply_match_validation(bot_module.db, match, ratings=ratings)
    assert outcome.weighted is True

    col = repository.get_elo_col(bot_module.db)
    assert col.find_one({"_id": "0:advanced"})["elo"] == 2026  # carry win +26
    assert col.find_one({"_id": "1:advanced"})["elo"] == 2020  # avg win +20
    assert col.find_one({"_id": "5:advanced"})["elo"] == 1985  # carry loss -15
    assert col.find_one({"_id": "6:advanced"})["elo"] == 1978  # feed loss -22


def test_pro_queue_missing_rating_falls_back_flat():
    import bot as bot_module

    match = _pro_match()
    # Player "2" has no rating -> flat ±20.
    ratings = {"0": 1.40}
    apply_match_validation(bot_module.db, match, ratings=ratings)

    col = repository.get_elo_col(bot_module.db)
    assert col.find_one({"_id": "0:open"})["elo"] == 2026  # weighted
    assert col.find_one({"_id": "2:open"})["elo"] == 2020  # flat +20


def test_pro_queue_zero_rating_falls_back_flat():
    import bot as bot_module

    match = _pro_match()
    ratings = {"5": 0.0}  # invalid rating -> flat loss
    apply_match_validation(bot_module.db, match, ratings=ratings)

    col = repository.get_elo_col(bot_module.db)
    assert col.find_one({"_id": "5:open"})["elo"] == 1980  # flat -20


def test_open_queue_applies_ratings():
    """Rating 2.0 weighting applies to the open queue too when ratings present."""
    import bot as bot_module

    match = _make_match(status="validated_a", elo=2400)  # queue_type=open
    ratings = {"0": 1.40, "5": 0.50}
    outcome = apply_match_validation(bot_module.db, match, ratings=ratings)
    assert outcome.weighted is True

    col = repository.get_elo_col(bot_module.db)
    # DB seeds new players at ELO_START=2000 (match_doc elo is avg-only).
    # Player 0 (team_a winner, rating 1.40) -> weighted +26.
    assert col.find_one({"_id": "0:open"})["elo"] == 2026
    # Player 5 (team_b loser, rating 0.50) -> weighted -22.
    assert col.find_one({"_id": "5:open"})["elo"] == 1978


def test_pro_queue_no_ratings_is_flat():
    """Pas de ratings (Henrik absent) -> ±20 plat, weighted False."""
    import bot as bot_module

    match = _pro_match()
    outcome = apply_match_validation(bot_module.db, match, ratings=None)
    assert outcome.weighted is False

    col = repository.get_elo_col(bot_module.db)
    assert col.find_one({"_id": "0:open"})["elo"] == 2020
    assert col.find_one({"_id": "5:open"})["elo"] == 1980
