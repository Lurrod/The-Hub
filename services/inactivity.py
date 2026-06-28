"""Ranking of players by inactivity (time elapsed since their last game).

Pure logic, without Discord or MongoDB dependency, so it can be tested
in isolation. The `elo_admin` cog uses it for the /inactivity command.

The `last_played` field is timestamped on every valid match by
`services.elo_updater`. A player without `last_played` has never played
a valid match since the introduction of this tracking: they are
considered the most inactive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_INACTIVITY_LIMIT = 25

# A player who has not played within this many days is hidden from the
# leaderboard.
LEADERBOARD_ACTIVE_DAYS = 7


def _as_utc(value: datetime) -> datetime:
    """Normalize a naive datetime to UTC (consistent with eligibility)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_active(
    last_played: datetime | None,
    now: datetime,
    *,
    max_idle_days: int = LEADERBOARD_ACTIVE_DAYS,
) -> bool:
    """True if the player played within `max_idle_days`.

    A player without `last_played` (never played) is considered inactive.
    The threshold is inclusive: exactly `max_idle_days` ago still counts
    as active.
    """
    if last_played is None:
        return False
    return _as_utc(now) - _as_utc(last_played) <= timedelta(days=max_idle_days)


def rank_by_inactivity(
    docs: Sequence[Mapping[str, Any]], *, limit: int = DEFAULT_INACTIVITY_LIMIT
) -> list[Mapping[str, Any]]:
    """Sort player docs from most inactive to least inactive.

    - Players without `last_played` are the most inactive -> placed first.
    - Others are sorted by `last_played` ascending (oldest first).
    - `name` serves as a tiebreaker for a deterministic order.

    Returns at most `limit` docs (empty list if `limit <= 0`).
    """

    def sort_key(doc: Mapping[str, Any]) -> tuple[int, float, str]:
        last = doc.get("last_played")
        name = str(doc.get("name", "")).lower()
        if last is None:
            return (0, 0.0, name)
        return (1, _as_utc(last).timestamp(), name)

    return sorted(docs, key=sort_key)[: max(limit, 0)]


def format_inactivity(last_played: datetime | None, now: datetime) -> str:
    """Inactivity text: "never played" or "Xd Xh Xm".

    A negative duration (clock skew) is clamped to zero.
    """
    if last_played is None:
        return "never played"
    delta = _as_utc(now) - _as_utc(last_played)
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m"
