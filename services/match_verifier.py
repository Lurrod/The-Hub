"""
Verification of a bot match via the HenrikDev API and computation of ACS
multipliers for per-player ELO adjustment.

Flow:
  1. Fetch the recent custom match history of the lobby leader.
  2. Find the match containing the 10 expected puuids, started after `after`.
  3. Compute each player's ACS and their multiplier (clamped to [0.7, 1.3])
     relative to the team average.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from services import elo_calc
from services.rating import RatingInputs, compute_rating_2_0
from services.riot_api import (
    HenrikDevClient,
    MatchPlayerStats,
    MatchSummary,
    RiotApiError,
)

CUSTOM_MODE_NAME: Final[str] = "Custom Game"
DEFAULT_MULT_MIN: Final[float] = 0.7
DEFAULT_MULT_MAX: Final[float] = 1.3


@dataclass(frozen=True)
class PlayerPerformance:
    user_id: str
    puuid: str
    acs: float
    multiplier: float
    win: bool


@dataclass(frozen=True)
class VerifiedMatch:
    matchid: str
    started_at: datetime
    winning_team: str  # "Red" or "Blue" (empty if draw)
    performances: tuple[PlayerPerformance, ...]


@dataclass(frozen=True)
class PlayerStatsExtended:
    """Full Rating 2.0 footprint for a single player in one match."""

    user_id: str
    puuid: str
    queue_type: str
    map_name: str
    agent: str
    team: str
    win: bool
    rounds_played: int
    acs: float
    kills: int
    deaths: int
    assists: int
    damage_made: int
    damage_received: int
    headshots: int
    bodyshots: int
    legshots: int
    multikills_2k: int
    multikills_3k: int
    multikills_4k: int
    multikills_5k: int
    first_kills: int
    first_deaths: int
    kast_rounds: int
    rating_2_0: float


def find_henrik_custom_match(
    client: HenrikDevClient,
    *,
    region: str,
    leader_name: str,
    leader_tag: str,
    expected_puuids: set[str],
    after: datetime,
    history_size: int = 10,
) -> MatchSummary | None:
    """Look up a custom match of `leader` that contains `expected_puuids`
    and started after `after`. Returns the `MatchSummary` or None.
    """
    try:
        history = client.get_match_history(
            region,
            leader_name,
            leader_tag,
            size=history_size,
            mode="custom",
        )
    except RiotApiError:
        return None

    for match in history:
        if match.mode != CUSTOM_MODE_NAME:
            continue
        if match.started_at < after:
            continue
        match_puuids = {p.puuid for p in match.players}
        if expected_puuids.issubset(match_puuids):
            return match
    return None


def compute_acs_multipliers(
    match: MatchSummary,
    *,
    team_a_uid_by_puuid: Mapping[str, str],
    team_b_uid_by_puuid: Mapping[str, str],
    mult_min: float = DEFAULT_MULT_MIN,
    mult_max: float = DEFAULT_MULT_MAX,
) -> VerifiedMatch:
    """Compute the ACS and clamped multiplier for each player, based on
    the team average (Henrik side: Red / Blue, mapped to the bot's
    teams a/b via the provided puuids)."""
    rounds = max(match.rounds_played, 1)
    if match.rounds_red > match.rounds_blue:
        winning = "Red"
    elif match.rounds_blue > match.rounds_red:
        winning = "Blue"
    else:
        winning = ""  # draw, edge case

    by_puuid: dict[str, MatchPlayerStats] = {p.puuid: p for p in match.players}

    perfs: list[PlayerPerformance] = []
    for team_uids in (team_a_uid_by_puuid, team_b_uid_by_puuid):
        labels = {by_puuid[pu].team for pu in team_uids if pu in by_puuid}
        if len(labels) != 1:
            continue  # inconsistent team on the Henrik side
        side = next(iter(labels))

        team_acs = [by_puuid[pu].score / rounds for pu in team_uids if pu in by_puuid]
        if not team_acs:
            continue
        avg_acs = sum(team_acs) / len(team_acs)
        if avg_acs <= 0:
            avg_acs = 1.0

        for pu, uid in team_uids.items():
            stats = by_puuid.get(pu)
            if stats is None:
                continue
            acs = stats.score / rounds
            raw = acs / avg_acs
            mult = max(mult_min, min(mult_max, raw))
            perfs.append(
                PlayerPerformance(
                    user_id=uid,
                    puuid=pu,
                    acs=acs,
                    multiplier=mult,
                    win=(side == winning),
                )
            )

    return VerifiedMatch(
        matchid=match.matchid,
        started_at=match.started_at,
        winning_team=winning,
        performances=tuple(perfs),
    )


def build_extended_stats(
    summary: MatchSummary,
    *,
    puuid_to_user_id: dict[str, str],
    queue_type: str,
) -> tuple[PlayerStatsExtended, ...]:
    """Translate a verified MatchSummary into one PlayerStatsExtended
    per linked Discord user. Players whose puuid is not in
    `puuid_to_user_id` are skipped.
    """
    if summary.rounds_red > summary.rounds_blue:
        winning_team = "Red"
    elif summary.rounds_blue > summary.rounds_red:
        winning_team = "Blue"
    else:
        winning_team = ""

    rounds = max(int(summary.rounds_played), 1)
    out: list[PlayerStatsExtended] = []
    for p in summary.players:
        uid = puuid_to_user_id.get(p.puuid)
        if not uid:
            continue
        rating = compute_rating_2_0(
            RatingInputs(
                rounds_played=rounds,
                kills=p.kills,
                deaths=p.deaths,
                assists=p.assists,
                damage_made=p.damage_made,
                kast_rounds=p.kast_rounds,
            )
        )
        out.append(
            PlayerStatsExtended(
                user_id=str(uid),
                puuid=p.puuid,
                queue_type=queue_type,
                map_name=summary.map_name,
                agent=p.agent,
                team=p.team,
                win=(p.team == winning_team) if winning_team else False,
                rounds_played=rounds,
                acs=p.score / rounds if rounds else 0.0,
                kills=p.kills,
                deaths=p.deaths,
                assists=p.assists,
                damage_made=p.damage_made,
                damage_received=p.damage_received,
                headshots=p.headshots,
                bodyshots=p.bodyshots,
                legshots=p.legshots,
                multikills_2k=p.multikills_2k,
                multikills_3k=p.multikills_3k,
                multikills_4k=p.multikills_4k,
                multikills_5k=p.multikills_5k,
                first_kills=p.first_kills,
                first_deaths=p.first_deaths,
                kast_rounds=p.kast_rounds,
                rating_2_0=rating,
            )
        )
    return tuple(out)


def ratings_by_uid(
    summary,
    puuid_to_user_id: Mapping[str, str],
    *,
    min_rounds: int = elo_calc.ELO_MIN_ROUNDS_FOR_WEIGHT,
) -> dict[str, float]:
    """Per-player Rating 2.0 keyed by Discord user_id, for ELO weighting.

    Returns an empty dict for matches shorter than `min_rounds` (forfeits,
    remakes) — those fall back to the flat ±20. Players whose puuid is not
    in `puuid_to_user_id`, or whose rating is non-positive, are skipped.
    """
    rounds = int(getattr(summary, "rounds_played", 0) or 0)
    if rounds < min_rounds:
        return {}

    out: dict[str, float] = {}
    for p in summary.players:
        uid = puuid_to_user_id.get(p.puuid)
        if uid is None:
            continue
        rating = compute_rating_2_0(
            RatingInputs(
                rounds_played=rounds,
                kills=int(p.kills or 0),
                deaths=int(p.deaths or 0),
                assists=int(p.assists or 0),
                damage_made=int(getattr(p, "damage_made", 0) or 0),
                kast_rounds=int(getattr(p, "kast_rounds", 0) or 0),
            )
        )
        if rating > 0:
            out[str(uid)] = rating
    return out


def compute_team_scores(
    summary,
    team_a_uid_by_puuid: Mapping[str, str],
    team_b_uid_by_puuid: Mapping[str, str],
) -> tuple[int | None, int | None]:
    """Map the Henrik Red/Blue round scores onto the bot's team_a / team_b.

    `summary` is a `MatchSummary` (`rounds_red`, `rounds_blue`, `players`
    each with a `.team` of "Red"/"Blue"). Returns `(score_a, score_b)`,
    or `(None, None)` when team_a's Henrik side cannot be unambiguously
    determined (no matched players, or players split across both sides).
    Mirrors the side-detection used by `compute_acs_multipliers`.
    """
    by_puuid = {p.puuid: p for p in summary.players}

    def _side(uid_by_puuid: Mapping[str, str]) -> str | None:
        labels = {by_puuid[pu].team for pu in uid_by_puuid if pu in by_puuid}
        return next(iter(labels)) if len(labels) == 1 else None

    side_a = _side(team_a_uid_by_puuid)
    if side_a not in ("Red", "Blue"):
        return (None, None)

    red = int(summary.rounds_red)
    blue = int(summary.rounds_blue)
    return (red, blue) if side_a == "Red" else (blue, red)


def compute_round_breakdown(
    summary,
    team_a_uid_by_puuid: Mapping[str, str],
    team_b_uid_by_puuid: Mapping[str, str],
) -> list[dict[str, str]]:
    """Per-round outcome mapped onto team_a / team_b for the website round bar.

    Returns an ordered list, one entry per round:
    ``{"winner": "a" | "b" | "", "end": "<Henrik end_type>"}``.
    `winner` is "a"/"b" relative to the bot's teams (resolved from team_a's
    Henrik Red/Blue side, like `compute_team_scores`), or "" when the side
    is ambiguous or the round winner is unknown. `end` is the raw Henrik
    end_type (e.g. "Eliminated", "Bomb defused") for the outcome icon.
    """
    by_puuid = {p.puuid: p for p in summary.players}

    def _side(uid_by_puuid: Mapping[str, str]) -> str | None:
        labels = {by_puuid[pu].team for pu in uid_by_puuid if pu in by_puuid}
        return next(iter(labels)) if len(labels) == 1 else None

    side_a = _side(team_a_uid_by_puuid)
    winners = tuple(getattr(summary, "round_winners", ()) or ())
    ends = tuple(getattr(summary, "round_end_types", ()) or ())

    out: list[dict[str, str]] = []
    for i, w in enumerate(winners):
        end = ends[i] if i < len(ends) else ""
        if side_a in ("Red", "Blue") and w in ("Red", "Blue"):
            winner = "a" if w == side_a else "b"
        else:
            winner = ""
        out.append({"winner": winner, "end": str(end)})
    return out
