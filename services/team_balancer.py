"""
Balance 2 teams of 5 from 10 players.

Algorithm: optimal brute-force.
  - C(10,5) = 252 partitions, but we pin player 0 in team A to avoid
    symmetric duplicates -> C(9,4) = 126 candidates.
  - Primary score    : minimize |sum(A.elo) - sum(B.elo)|
  - Tie-breaker      : minimize |max(A.elo) - max(B.elo)| (no solo stack)
  - Tie-breaker 2    : lexicographic order of IDs (deterministic)

Complexity: O(126 * 10) ~= 1.3k operations. Well under a millisecond.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

TEAM_SIZE: Final[int] = 5
TOTAL_PLAYERS: Final[int] = 10


@dataclass(frozen=True)
class Player:
    id: int  # Discord user id
    name: str
    elo: int


@dataclass(frozen=True)
class BalancedTeams:
    team_a: tuple[Player, ...]
    team_b: tuple[Player, ...]
    elo_diff: int  # |sum(A) - sum(B)|
    peak_diff: int  # |max(A) - max(B)|

    @property
    def total_a(self) -> int:
        return sum(p.elo for p in self.team_a)

    @property
    def total_b(self) -> int:
        return sum(p.elo for p in self.team_b)


def balance_teams(players: Iterable[Player]) -> BalancedTeams:
    """Returns the most balanced split.

    Complexity:
        Pins ``pool[0]`` in team A to eliminate symmetric partitions,
        then iterates ``C(9, 4) = 126`` combinations. Each scored in O(10).
        Total ~1.3k ops — well under a millisecond for any realistic input.

    Raises:
        ValueError if ``len(players) != 10`` or if there are duplicate IDs.
    """
    pool = tuple(players)
    if len(pool) != TOTAL_PLAYERS:
        raise ValueError(f"Exactly {TOTAL_PLAYERS} players required, received {len(pool)}")
    if len({p.id for p in pool}) != TOTAL_PLAYERS:
        raise ValueError("Duplicate IDs detected in the player list")

    best: BalancedTeams | None = None
    best_key: tuple[int, int, tuple[int, ...]] | None = None

    # We pin pool[0] in team A to only generate unique partitions.
    # We pick 4 others among pool[1..9] -> C(9,4) = 126 combinations.
    indices_rest = range(1, TOTAL_PLAYERS)
    for combo in itertools.combinations(indices_rest, TEAM_SIZE - 1):
        a_idx = (0, *combo)
        a_set = set(a_idx)
        team_a = tuple(pool[i] for i in a_idx)
        team_b = tuple(pool[i] for i in range(TOTAL_PLAYERS) if i not in a_set)

        sum_a = sum(p.elo for p in team_a)
        sum_b = sum(p.elo for p in team_b)
        elo_diff = abs(sum_a - sum_b)

        max_a = max(p.elo for p in team_a)
        max_b = max(p.elo for p in team_b)
        peak_diff = abs(max_a - max_b)

        # Tie-breaker 2: ID order (deterministic for tests)
        id_signature = tuple(sorted(p.id for p in team_a))
        key = (elo_diff, peak_diff, id_signature)

        if best_key is None or key < best_key:
            best_key = key
            best = BalancedTeams(
                team_a=team_a,
                team_b=team_b,
                elo_diff=elo_diff,
                peak_diff=peak_diff,
            )

    if best is None:
        raise RuntimeError("balance_teams: no valid combination found (need >= 10 players)")
    return best


def format_teams(teams: BalancedTeams) -> str:
    """Compact text format for log/debug."""
    a_str = ", ".join(f"{p.name}({p.elo})" for p in teams.team_a)
    b_str = ", ".join(f"{p.name}({p.elo})" for p in teams.team_b)
    return (
        f"Team A [{teams.total_a}] : {a_str}\n"
        f"Team B [{teams.total_b}] : {b_str}\n"
        f"diff={teams.elo_diff}  peak_diff={teams.peak_diff}"
    )
