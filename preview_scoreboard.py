"""Local-only preview generator for the VLR-style scoreboard.

Run::

    python preview_scoreboard.py

Writes ``preview_scoreboard.png`` in the repo root. Not loaded by the bot.
"""

from __future__ import annotations

from services.scoreboard_img import generate_scoreboard


def _player(
    name: str,
    *,
    agent: str,
    rating: float,
    acs: int,
    kills: int,
    deaths: int,
    assists: int,
    kast: float,
    adr: float,
    hs: float,
    fk: int,
    fd: int,
) -> dict:
    return {
        "name": name,
        "agent": agent,
        "rating_2_0": rating,
        "acs": acs,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kast_pct": kast,
        "adr": adr,
        "hs_pct": hs,
        "first_kills": fk,
        "first_deaths": fd,
    }


team_a = [
    _player(
        "sanoj",
        agent="Jett",
        rating=1.42,
        acs=194,
        kills=14,
        deaths=8,
        assists=6,
        kast=76,
        adr=142,
        hs=29,
        fk=1,
        fd=2,
    ),
    _player(
        "Kring",
        agent="Omen",
        rating=1.31,
        acs=256,
        kills=18,
        deaths=11,
        assists=5,
        kast=81,
        adr=163,
        hs=17,
        fk=2,
        fd=2,
    ),
    _player(
        "aztex",
        agent="Raze",
        rating=1.26,
        acs=233,
        kills=18,
        deaths=13,
        assists=9,
        kast=81,
        adr=142,
        hs=40,
        fk=3,
        fd=0,
    ),
    _player(
        "Luid",
        agent="Sova",
        rating=1.17,
        acs=255,
        kills=19,
        deaths=12,
        assists=1,
        kast=57,
        adr=166,
        hs=46,
        fk=4,
        fd=5,
    ),
    _player(
        "turkish",
        agent="Cypher",
        rating=0.66,
        acs=120,
        kills=8,
        deaths=15,
        assists=6,
        kast=67,
        adr=82,
        hs=30,
        fk=2,
        fd=0,
    ),
]

team_b = [
    _player(
        "tecao",
        agent="Astra",
        rating=1.15,
        acs=211,
        kills=17,
        deaths=16,
        assists=2,
        kast=57,
        adr=148,
        hs=42,
        fk=3,
        fd=2,
    ),
    _player(
        "shiyande",
        agent="Reyna",
        rating=0.93,
        acs=184,
        kills=14,
        deaths=15,
        assists=2,
        kast=52,
        adr=120,
        hs=19,
        fk=5,
        fd=4,
    ),
    _player(
        "AstroDarkin",
        agent="Sage",
        rating=0.79,
        acs=160,
        kills=11,
        deaths=15,
        assists=3,
        kast=71,
        adr=117,
        hs=34,
        fk=0,
        fd=2,
    ),
    _player(
        "quev",
        agent="Yoru",
        rating=0.68,
        acs=108,
        kills=9,
        deaths=14,
        assists=6,
        kast=52,
        adr=70,
        hs=8,
        fk=1,
        fd=1,
    ),
    _player(
        "Davizao",
        agent="Killjoy",
        rating=0.43,
        acs=114,
        kills=8,
        deaths=18,
        assists=5,
        kast=48,
        adr=76,
        hs=29,
        fk=1,
        fd=3,
    ),
]

# ELO gained/lost per player: team A wins (13-8), weighted by Rating 2.0.
from services.elo_calc import compute_weighted_delta  # noqa: E402

for _p in team_a:
    _p["elo_delta"] = compute_weighted_delta(_p["rating_2_0"], win=True)
for _p in team_b:
    _p["elo_delta"] = compute_weighted_delta(_p["rating_2_0"], win=False)

# Henrik convention: winning_team is "Red" or "Blue" per round.
round_winners = [
    "Red",
    "Red",
    "Blue",
    "Blue",
    "Blue",
    "Blue",
    "Red",
    "Red",
    "Red",
    "Red",
    "Red",
    "Red",
    "Blue",
    "Blue",
    "Blue",
    "Red",
    "Red",
    "Blue",
    "Blue",
    "Blue",
    "Blue",
]
round_end_types = [
    "Eliminated",
    "Bomb detonated",
    "Eliminated",
    "Bomb defused",
    "Round timer expired",
    "Eliminated",
    "Bomb detonated",
    "Eliminated",
    "Eliminated",
    "Bomb defused",
    "Eliminated",
    "Bomb detonated",
    "Bomb defused",
    "Eliminated",
    "Round timer expired",
    "Eliminated",
    "Bomb detonated",
    "Eliminated",
    "Bomb defused",
    "Eliminated",
    "Eliminated",
]

buf = generate_scoreboard(
    map_name="Ascent",
    rounds_a=13,
    rounds_b=8,
    team_a_label="Team A",
    team_b_label="Team B",
    team_a_players=team_a,
    team_b_players=team_b,
    queue_label="Pro Queue",
    round_winners=round_winners,
    round_end_types=round_end_types,
)
with open("preview_scoreboard.png", "wb") as f:
    f.write(buf.getvalue())
print("Wrote preview_scoreboard.png")
