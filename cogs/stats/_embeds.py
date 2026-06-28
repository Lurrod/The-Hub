"""Discord embed builders for /stats. Pure functions: no I/O, no
Discord interactions. Take dict-shaped DB rows + a Member and return
`discord.Embed`. Easy to unit-test.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import discord

from services.rating import RatingInputs, compute_rating_2_0

# Display label per queue_type. Kept local to avoid a circular import
# with cogs/elo_admin.py; must stay in sync with the canonical labels
# there.
QUEUE_LABELS = {
    "open": "Open Queue",
    "advanced": "Advanced Queue",
}


def _winrate(wins: int, losses: int) -> tuple[float, int]:
    total = wins + losses
    if total == 0:
        return 0.0, 0
    return round((wins / total) * 100.0, 1), total


def _rating_from_agg(agg: Mapping[str, Any]) -> float:
    rounds = int(agg.get("rounds_played", 0) or 0)
    if rounds <= 0:
        return 0.0
    return compute_rating_2_0(
        RatingInputs(
            rounds_played=rounds,
            kills=int(agg.get("kills", 0)),
            deaths=int(agg.get("deaths", 0)),
            assists=int(agg.get("assists", 0)),
            damage_made=int(agg.get("damage_made", 0)),
            kast_rounds=int(agg.get("kast_rounds", 0)),
        )
    )


def build_overview_embed(
    *,
    elo_doc: Mapping[str, Any] | None,
    rank: int | None,
    agg: Mapping[str, Any] | None,
    member,
    queue_type: str,
) -> discord.Embed:
    e = discord.Embed(
        title=f"📊 {member.display_name} — {QUEUE_LABELS.get(queue_type, queue_type)}",
        color=0x3498DB,
        timestamp=datetime.now(UTC),
    )
    e.set_thumbnail(url=member.display_avatar.url)

    if elo_doc is not None:
        elo = int(elo_doc.get("elo", 0))
        wins = int(elo_doc.get("wins", 0))
        losses = int(elo_doc.get("losses", 0))
        winrate, _total = _winrate(wins, losses)
        e.add_field(name="🏅 ELO", value=f"**{elo}**", inline=True)
        e.add_field(
            name="🏆 Classement",
            value=f"**#{rank}**" if rank else "—",
            inline=True,
        )
        e.add_field(
            name="📈 Winrate",
            value=f"**{winrate}%** ({wins}V / {losses}D)",
            inline=True,
        )

    if agg is None:
        e.set_footer(text="Les stats Rating 2.0 commencent à partir de votre prochain match.")
        return e

    rounds = max(int(agg.get("rounds_played", 0) or 0), 1)
    kills = int(agg.get("kills", 0))
    deaths = int(agg.get("deaths", 0))
    assists = int(agg.get("assists", 0))
    damage = int(agg.get("damage_made", 0))
    headshots = int(agg.get("headshots", 0))
    bodyshots = int(agg.get("bodyshots", 0))
    legshots = int(agg.get("legshots", 0))
    kast_rounds = int(agg.get("kast_rounds", 0))
    games = int(agg.get("games", 0))

    rating = _rating_from_agg(agg)
    kpr = kills / rounds
    dpr = deaths / rounds
    adr = damage / rounds
    kast_pct = (kast_rounds / rounds) * 100.0
    # ACS rough proxy from aggregate: damage_made + 50 per kill, /rounds.
    # (True ACS is per-round and not preserved post-aggregation; this is
    # a reasonable display surrogate.)
    acs = (damage + kills * 50) / rounds
    total_shots = headshots + bodyshots + legshots
    hs_pct = (headshots / total_shots * 100.0) if total_shots else 0.0

    e.add_field(name="⭐ Rating 2.0", value=f"**{rating:.2f}**", inline=True)
    e.add_field(name="🎮 Parties", value=f"**{games}**", inline=True)
    e.add_field(name="​", value="​", inline=True)  # filler

    e.add_field(name="K / D / A", value=f"{kills} / {deaths} / {assists}", inline=True)
    e.add_field(name="KPR / DPR", value=f"{kpr:.2f} / {dpr:.2f}", inline=True)
    e.add_field(name="ADR", value=f"{adr:.1f}", inline=True)
    e.add_field(name="HS%", value=f"{hs_pct:.1f}%", inline=True)
    e.add_field(name="KAST", value=f"{kast_pct:.1f}%", inline=True)
    e.add_field(name="ACS", value=f"{acs:.0f}", inline=True)

    e.set_footer(text=f"Page 1/2 • {games} parties")
    return e


def build_details_embed(
    *,
    agg: Mapping[str, Any],
    member,
    queue_type: str,
) -> discord.Embed:
    e = discord.Embed(
        title=f"📊 {member.display_name} — Détails ({QUEUE_LABELS.get(queue_type, queue_type)})",
        color=0x3498DB,
        timestamp=datetime.now(UTC),
    )
    e.set_thumbnail(url=member.display_avatar.url)

    rounds = max(int(agg.get("rounds_played", 0) or 0), 1)
    fk = int(agg.get("first_kills", 0))
    fd = int(agg.get("first_deaths", 0))
    fk_ratio = (fk / fd) if fd else float(fk)
    fk_pct = (fk / rounds) * 100.0

    e.add_field(
        name="Multikills",
        value=(
            f"2K **{int(agg.get('multikills_2k', 0))}**   "
            f"3K **{int(agg.get('multikills_3k', 0))}**   "
            f"4K **{int(agg.get('multikills_4k', 0))}**   "
            f"5K **{int(agg.get('multikills_5k', 0))}**"
        ),
        inline=False,
    )
    e.add_field(
        name="Duels d'ouverture",
        value=f"FK **{fk}** / FD **{fd}**   ratio **{fk_ratio:.2f}**",
        inline=False,
    )
    kpr = int(agg.get("kills", 0)) / rounds
    apr = int(agg.get("assists", 0)) / rounds
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    e.add_field(name="Impact", value=f"**{impact:.2f}**", inline=True)
    e.add_field(
        name="First Kill %",
        value=f"**{fk_pct:.1f}%** des rounds",
        inline=True,
    )
    e.add_field(
        name="HS / Corps / Jambes",
        value=(
            f"{int(agg.get('headshots', 0))} / "
            f"{int(agg.get('bodyshots', 0))} / "
            f"{int(agg.get('legshots', 0))}"
        ),
        inline=False,
    )
    games = int(agg.get("games", 0))
    e.set_footer(text=f"Page 2/2 • {games} parties")
    return e
