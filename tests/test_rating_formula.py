"""Tests for services/rating.py — Rating 2.0 / Impact formula."""

from __future__ import annotations

import math

from services.rating import RatingInputs, compute_impact, compute_rating_2_0


def test_rating_zero_rounds_returns_zero():
    inputs = RatingInputs(
        rounds_played=0,
        kills=10,
        deaths=5,
        assists=3,
        damage_made=1000,
        kast_rounds=8,
    )
    assert compute_rating_2_0(inputs) == 0.0


def test_rating_perfect_zero_input():
    inputs = RatingInputs(
        rounds_played=24,
        kills=0,
        deaths=0,
        assists=0,
        damage_made=0,
        kast_rounds=0,
    )
    # All-zero inputs: KAST/KPR/DPR/APR/ADR all zero. The raw linear
    # combination evaluates negative (-0.001 + 0.2372 * (-0.41) ≈ -0.098),
    # but Rating 2.0 is floored at 0.01 so it is never shown as negative.
    assert compute_rating_2_0(inputs) == 0.01


def test_rating_never_negative():
    """A heavy-loss statline must never produce a zero/negative rating;
    the score is floored at 0.01 (the floor the scoreboard relies on)."""
    inputs = RatingInputs(
        rounds_played=24,
        kills=0,
        deaths=24,
        assists=0,
        damage_made=0,
        kast_rounds=0,
    )
    assert compute_rating_2_0(inputs) == 0.01


def test_rating_high_frag_above_one():
    inputs = RatingInputs(
        rounds_played=24,
        kills=30,
        deaths=10,
        assists=5,
        damage_made=5000,
        kast_rounds=22,
    )
    r = compute_rating_2_0(inputs)
    assert r > 1.0


def test_rating_low_frag_below_one():
    inputs = RatingInputs(
        rounds_played=24,
        kills=8,
        deaths=20,
        assists=2,
        damage_made=1500,
        kast_rounds=8,
    )
    r = compute_rating_2_0(inputs)
    assert r < 1.0


def test_impact_formula_matches_hltv_constants():
    # KPR=1.0, APR=0.5  ->  Impact = 2.13 + 0.21 - 0.41 = 1.93
    impact = compute_impact(kpr=1.0, apr=0.5)
    assert math.isclose(impact, 1.93, abs_tol=1e-6)


def test_rating_average_player_near_one():
    inputs = RatingInputs(
        rounds_played=24,
        kills=18,
        deaths=15,
        assists=4,
        damage_made=3500,
        kast_rounds=17,
    )
    r = compute_rating_2_0(inputs)
    # Solid frag profile (KPR 0.75, DPR 0.625, KAST 71%, ADR ~146).
    # After the Valorant recalibration (ADR coefficient rescaled and
    # intercept recentred) this average statline maps to ~1.00, the
    # scale the scoreboard colours already assume. The band leaves
    # headroom for minor rounding without falsifying the direction.
    assert 0.95 < r < 1.05
