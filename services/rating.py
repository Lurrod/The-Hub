"""HLTV 2.0-equivalent Rating formula adapted for Valorant.

The formula keeps the CSGO/CS2 Rating 2.0 structure and most of its
calibration parameters from the original article. Two constants are
adapted to the Valorant damage scale so that an average player lands
near 1.00 (instead of the inflated ~1.38 the raw CS constants produce
on Valorant data):

  * ``_ADR_C`` is rescaled by the CS/Valorant average-ADR ratio
    (~78 / ~146 ≈ 0.534). Valorant players have 100 HP + up to 50
    shield, so per-round damage runs roughly twice CS; the raw CS
    coefficient over-rewards damage on Valorant inputs.
  * ``_INTERCEPT`` is then recentred so the average statline maps to
    1.00 on the same scale the scoreboard colours already assume
    (green >= 1.10, red < 0.85).

All other inputs are sourced from Henrik's Valorant match data.

Reference: https://www.hltv.org/news/20695/introducing-rating-20
"""

from __future__ import annotations

from dataclasses import dataclass

_INTERCEPT = -0.001  # recentred for Valorant so an average statline ~= 1.00
_KAST_C = 0.0073
_KPR_C = 0.3591
_DPR_C = -0.5329
_IMPACT_C = 0.2372
_ADR_C = 0.00171  # CS 0.0032 rescaled by ~78/146 for Valorant's higher ADR

_IMPACT_KPR_C = 2.13
_IMPACT_APR_C = 0.42
_IMPACT_OFFSET = -0.41

# Lower bound for a Rating 2.0 score. A heavy-loss statline (high deaths,
# no kills/damage) drives the raw linear combination below zero, which
# reads as broken on the scoreboard. We floor it at a small positive value
# so a rating is never shown as zero or negative.
_MIN_RATING = 0.01


@dataclass(frozen=True)
class RatingInputs:
    """Raw counters needed to evaluate a Rating 2.0 score.

    All fields are *totals* over the rounds the player was in. The
    formula divides each by `rounds_played` to get per-round rates.
    """

    rounds_played: int
    kills: int
    deaths: int
    assists: int
    damage_made: int
    kast_rounds: int


def compute_impact(*, kpr: float, apr: float) -> float:
    """Impact rating: kill-pressure component of Rating 2.0.

    Captures multi-kill / opening-duel weight indirectly via KPR and
    assist participation via APR.
    """
    return _IMPACT_KPR_C * kpr + _IMPACT_APR_C * apr + _IMPACT_OFFSET


def compute_rating_2_0(inputs: RatingInputs) -> float:
    """Rating 2.0 score, floored at 0.01 so it is never zero/negative.

    Returns 0.0 only when `rounds_played <= 0` (no data to score)."""
    rounds = inputs.rounds_played
    if rounds <= 0:
        return 0.0
    kast = (inputs.kast_rounds / rounds) * 100.0
    kpr = inputs.kills / rounds
    dpr = inputs.deaths / rounds
    apr = inputs.assists / rounds
    adr = inputs.damage_made / rounds
    impact = compute_impact(kpr=kpr, apr=apr)
    rating = (
        _KAST_C * kast
        + _KPR_C * kpr
        + _DPR_C * dpr
        + _IMPACT_C * impact
        + _ADR_C * adr
        + _INTERCEPT
    )
    return max(_MIN_RATING, rating)
