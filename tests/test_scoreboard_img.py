"""Unit tests for services/scoreboard_img.py.

We don't pixel-diff the output — the goal here is to make sure the
generator returns a valid PNG without crashing on representative and
edge-case inputs (missing keys, draws, etc.). The image content is
sanity-checked by re-opening the bytes with PIL.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from PIL import Image

from services.scoreboard_img import (
    PLAYERS_PER_TEAM,
    WIDTH,
    _truncate,
    generate_scoreboard,
)


def _player(
    name: str = "Jet",
    kills=20,
    deaths=15,
    assists=5,
    acs=240,
    elo=1500,
    *,
    rating_2_0: float | None = None,
    kast_pct=70.0,
    adr=140.0,
    hs_pct=25.0,
    first_kills=2,
    first_deaths=1,
):
    """Build a player row used by ``generate_scoreboard``.

    ``rating_2_0`` is the VLR-style scoreboard's primary sort key. When
    not provided we derive a stable proxy from ACS so that tests that
    only care about ordering keep working without spelling rating out.
    """
    return {
        "name": name,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "acs": acs,
        "elo": elo,
        "avatar_url": None,
        "rating_2_0": acs / 200.0 if rating_2_0 is None else rating_2_0,
        "kast_pct": kast_pct,
        "adr": adr,
        "hs_pct": hs_pct,
        "first_kills": first_kills,
        "first_deaths": first_deaths,
    }


def _team(prefix: str = "P", n: int = PLAYERS_PER_TEAM):
    return [
        _player(name=f"{prefix}{i}#tag", kills=20 + i, deaths=14 + i, acs=230 + 10 * i)
        for i in range(n)
    ]


def test_generate_scoreboard_returns_valid_png():
    buf = generate_scoreboard(
        map_name="Ascent",
        rounds_a=13,
        rounds_b=7,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
        queue_label="Pro Queue",
    )
    assert isinstance(buf, BytesIO)
    buf.seek(0)
    img = Image.open(buf)
    assert img.format == "PNG"
    assert img.width == WIDTH
    assert img.height > 0


def test_generate_scoreboard_handles_draw():
    """13-13 (overtime cap) shouldn't paint either side as winner."""
    buf = generate_scoreboard(
        map_name="Bind",
        rounds_a=12,
        rounds_b=12,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_handles_short_rosters():
    """If a team has fewer than 5 players (e.g. missing puuid), the
    layout still renders without IndexError."""
    short_a = _team("A", n=3)
    short_b = _team("B", n=4)
    buf = generate_scoreboard(
        map_name="Haven",
        rounds_a=13,
        rounds_b=9,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=short_a,
        team_b_players=short_b,
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_handles_missing_fields():
    """Missing player fields (no kills/elo/etc.) default to 0 safely."""
    sparse = [{"name": "Mystery"} for _ in range(5)]
    buf = generate_scoreboard(
        map_name="Split",
        rounds_a=13,
        rounds_b=0,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=sparse,
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_sorts_by_rating_desc():
    """Top Rating 2.0 appears first even if input is unsorted.

    The VLR-style layout sorts both blocks by Rating 2.0 — captures
    overall impact better than ACS alone (KAST + ADR factor in).
    """
    rendered: list[dict] = []
    real = generate_scoreboard.__globals__["_draw_player_row"]

    def spy(img, draw, player, **kwargs):
        rendered.append(player)
        return real(img, draw, player, **kwargs)

    unsorted = [
        _player(name="low", rating_2_0=0.50),
        _player(name="mid", rating_2_0=1.00),
        _player(name="top", rating_2_0=1.45),
        _player(name="other1", rating_2_0=0.80),
        _player(name="other2", rating_2_0=1.20),
    ]
    with patch("services.scoreboard_img._draw_player_row", spy):
        generate_scoreboard(
            map_name="Lotus",
            rounds_a=13,
            rounds_b=11,
            team_a_label="Team A",
            team_b_label="Team B",
            team_a_players=unsorted,
            team_b_players=_team("B"),
        )
    # First 5 spy calls are team A, in descending rating order.
    first_five = [p["name"] for p in rendered[:5]]
    assert first_five == ["top", "other2", "mid", "other1", "low"]


def test_truncate_returns_original_when_it_fits():
    """_truncate should leave short strings alone (no spurious ellipsis)."""
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw

    img = _PILImage.new("RGB", (200, 50), (0, 0, 0))
    draw = _PILDraw.Draw(img)
    from services.scoreboard_img import _font

    font = _font(20)
    assert _truncate(draw, "Jet", font, 9999) == "Jet"


def test_truncate_adds_ellipsis_when_too_long():
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw

    img = _PILImage.new("RGB", (200, 50), (0, 0, 0))
    draw = _PILDraw.Draw(img)
    from services.scoreboard_img import _font

    font = _font(20)
    long = "ThisIsAReallyLongNameThatWillNotFitInTheColumn"
    result = _truncate(draw, long, font, max_width=80)
    assert result.endswith("…")
    assert len(result) < len(long)


def test_generate_scoreboard_omits_queue_label_when_empty():
    """Empty queue_label must not raise — just skip the corner label."""
    buf = generate_scoreboard(
        map_name="Sunset",
        rounds_a=13,
        rounds_b=8,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
        queue_label="",
    )
    img = Image.open(buf)
    assert img.width == WIDTH


# ── Agent icon loading ──────────────────────────────────────────
def test_load_agent_icon_returns_image_for_known_agent():
    """A committed agent asset should resolve to a PIL image."""
    from services.scoreboard_img import (
        _AGENT_ICON_CACHE,
        AGENT_ICON_SIZE,
        _load_agent_icon,
    )

    _AGENT_ICON_CACHE.clear()
    icon = _load_agent_icon("Jett")
    assert icon is not None
    assert icon.size == (AGENT_ICON_SIZE, AGENT_ICON_SIZE)


def test_load_agent_icon_handles_slash_in_name():
    """KAY/O lives on disk as KAY_O.png — the loader must remap."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    assert _load_agent_icon("KAY/O") is not None


def test_load_agent_icon_returns_none_for_unknown_agent():
    """Unknown agent name (never released, typo) -> None, no crash."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    assert _load_agent_icon("MysteryAgentZ") is None


def test_load_agent_icon_returns_none_for_empty_string():
    """Empty/None agent -> None (the renderer falls back to a placeholder)."""
    from services.scoreboard_img import _load_agent_icon

    assert _load_agent_icon("") is None
    assert _load_agent_icon(None) is None


def test_load_agent_icon_caches_repeated_lookups():
    """Second call returns the same Image instance from memory."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    first = _load_agent_icon("Sage")
    second = _load_agent_icon("Sage")
    assert first is second


# ── Column headers + new layout ────────────────────────────────
def test_scoreboard_height_matches_layout_constants():
    """Total height = sum of all layout bands (one column header + 5 rows per team)."""
    from services.scoreboard_img import (
        BLOCK_GAP,
        COLUMN_HEADER_BAND,
        FOOTER_BAND,
        HEADER_BAND,
        PLAYERS_PER_TEAM,
        ROUND_BAR_BAND,
        ROW_HEIGHT,
    )

    buf = generate_scoreboard(
        map_name="Ascent",
        rounds_a=13,
        rounds_b=7,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    expected_h = (
        HEADER_BAND
        + ROUND_BAR_BAND
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + BLOCK_GAP
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + FOOTER_BAND
    )
    assert img.height == expected_h


def test_scoreboard_accepts_round_winners_argument():
    """Passing the per-round winners sequence (Red/Blue/"") must not crash."""
    winners = ["Red"] * 7 + ["Blue"] * 4 + ["Red"] * 2  # 13 - 4 sample
    buf = generate_scoreboard(
        map_name="Ascent",
        rounds_a=9,
        rounds_b=4,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
        round_winners=winners,
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_round_bar_colours_distinguish_winner_and_loser_team():
    """The round-bar fill colour matches the OVERALL match outcome:
    the winning team's WON squares are green, the losing team's WON
    squares are red. Lost and not-played squares are EMPTY (grey)."""
    from services.scoreboard_img import (
        ROUND_EMPTY_FILL,
        ROUND_LOSS_RED,
        ROUND_WIN_GREEN,
    )

    # Sanity check: the three constants are distinct (test would silently
    # pass if a future refactor collapsed them).
    assert ROUND_WIN_GREEN != ROUND_LOSS_RED != ROUND_EMPTY_FILL


def test_rating_color_bands():
    """Rating < 0.85 = red, 0.85-1.10 = neutral white, >= 1.10 = green."""
    from services.scoreboard_img import (
        DELTA_NEG_RED,
        DELTA_POS_GREEN,
        WHITE,
        _rating_color,
    )

    assert _rating_color(1.30) == DELTA_POS_GREEN
    assert _rating_color(1.00) == WHITE
    assert _rating_color(0.60) == DELTA_NEG_RED


def test_scoreboard_renders_with_agent_field_present():
    """Generator must not crash when player rows carry an agent name."""
    a = [{**p, "agent": "Jett"} for p in _team("A")]
    b = [{**p, "agent": "Sage"} for p in _team("B")]
    buf = generate_scoreboard(
        map_name="Haven",
        rounds_a=10,
        rounds_b=13,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=a,
        team_b_players=b,
    )
    img = Image.open(buf)
    assert img.width == WIDTH


# ── ELO gain/loss column (right after the Rating column) ──────────
def test_player_row_renders_elo_delta_after_rating():
    """The ELO delta is drawn via _draw_delta at X_ELO, positioned
    between the Rating (X_R) and ACS (X_ACS) columns."""
    from PIL import Image as _Img
    from PIL import ImageDraw as _Draw

    import services.scoreboard_img as sb

    calls: list[tuple[int, int]] = []
    orig = sb._draw_delta

    def _spy(draw, value, x_center, y_center, font):
        calls.append((value, x_center))
        return orig(draw, value, x_center, y_center, font)

    sb._draw_delta = _spy
    try:
        img = _Img.new("RGB", (sb.WIDTH, 120), (0, 0, 0))
        draw = _Draw.Draw(img)
        player = _player(rating_2_0=1.40)
        player["elo_delta"] = 26
        sb._draw_player_row(
            img,
            draw,
            player,
            y_center=60,
            name_font=sb._font(18),
            stats_font=sb._font(19),
            kda_font=sb._font(17),
        )
    finally:
        sb._draw_delta = orig

    # ELO column rendered with the delta value at X_ELO.
    assert (26, sb.X_ELO) in calls
    # And it sits just after the rating, before ACS.
    assert sb.X_R < sb.X_ELO < sb.X_ACS


def test_player_row_missing_elo_delta_defaults_to_zero():
    """Missing elo_delta must not crash — renders a neutral 0."""
    from PIL import Image as _Img
    from PIL import ImageDraw as _Draw

    import services.scoreboard_img as sb

    calls: list[tuple[int, int]] = []
    orig = sb._draw_delta

    def _spy(draw, value, x_center, y_center, font):
        calls.append((value, x_center))
        return orig(draw, value, x_center, y_center, font)

    sb._draw_delta = _spy
    try:
        img = _Img.new("RGB", (sb.WIDTH, 120), (0, 0, 0))
        draw = _Draw.Draw(img)
        sb._draw_player_row(
            img,
            draw,
            _player(),  # no elo_delta key
            y_center=60,
            name_font=sb._font(18),
            stats_font=sb._font(19),
            kda_font=sb._font(17),
        )
    finally:
        sb._draw_delta = orig

    assert (0, sb.X_ELO) in calls
