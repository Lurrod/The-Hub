"""
Per-match scoreboard image generation via Pillow.

VLR-style layout (stacked teams, no flags, no team subtitles per player).

Posted by the bot in the queue's results channel (pro-results / etc.)
once Henrik finds the custom and we have the match stats.

Layout (top → bottom):
    1. Header band       — team label / score / map / score / team label
    2. Round bar         — N small squares per team showing per-round W/L
    3. Team A block      — column header row + 5 player rows
    4. Team B block      — column header row + 5 player rows
    5. Footer            — "Play'IT Matchmaking Bot"

Columns (per player row):
    name | agent | R | ACS | K / D / A | +/- | KAST | ADR | HS% | FK | FD | +/-

Agent icons are loaded from ``assets/agents/<AgentName>.png`` (committed
to the repo). ``KAY/O`` maps to ``KAY_O.png``. Missing icon → grey
placeholder.

Input format (per player, both teams). Optional fields fall back to 0:

    {
        "name":         str,            # Discord display name
        "agent":        str | None,     # Valorant agent name (icon lookup)
        "rating_2_0":   float,          # HLTV 2.0-equivalent (services.rating)
        "acs":          int,
        "kills":        int,
        "deaths":       int,
        "assists":      int,
        "kast_pct":     float,          # already a percentage (0-100)
        "adr":          float,          # avg damage per round
        "hs_pct":       float,          # already a percentage (0-100)
        "first_kills":  int,
        "first_deaths": int,
    }

``round_winners`` is an ordered sequence of "Red" / "Blue" / "" (one
entry per round); team A is "Red", team B is "Blue".
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ── Layout ────────────────────────────────────────────────────────
WIDTH = 1500
PLAYERS_PER_TEAM = 5

HEADER_BAND = 96  # team labels + big score + map name
ROUND_BAR_BAND = 60  # the 24-square W/L bar
COLUMN_HEADER_BAND = 36  # per-team column header row
ROW_HEIGHT = 56  # per-player row
BLOCK_GAP = 14  # vertical gap between team A block and team B block
FOOTER_BAND = 38

# Per-row column positions (absolute X, since the layout is full-width).
PAD_LEFT = 28
ACCENT_W = 4
NAME_X = PAD_LEFT + ACCENT_W + 8
AGENT_ICON_SIZE = 40
AGENT_X = NAME_X + 220  # agent icon sits AFTER the name, like VLR
NAME_MAX_W = AGENT_X - NAME_X - 16

# Stat column centers (absolute X).
X_R = 520  # Rating 2.0 (shifted left into the empty name gap)
X_ELO = 610  # ELO +/- — 90px after R
X_ACS = 700  # 90px after ELO (right block pulled left to match)
X_KDA = 845
X_KD = 970
X_KAST = 1055
X_ADR = 1145
X_HS = 1230
X_FK = 1300
X_FD = 1360
X_FKFD = 1415

# ── Colors ────────────────────────────────────────────────────────
BG = (16, 22, 31)
HEADER_BG = (20, 27, 38)
ROW_BG_A = (22, 28, 38)
ROW_BG_B = (26, 33, 44)
ROW_BG_HEADER = (24, 31, 42)
SEPARATOR = (38, 46, 60)
DIVIDER = (45, 55, 72)

WHITE = (240, 244, 252)
SOFT_GRAY = (160, 170, 188)
DIM_GRAY = (108, 118, 138)
HEADER_GRAY = (140, 150, 168)

# Win/lose / per-round status.
WIN_GREEN = (52, 188, 138)
LOSE_RED = (208, 80, 88)
# Round bar: each team's WON rounds are filled with the team's overall
# colour (green if they won the match, red if they lost). Lost or
# not-played rounds stay empty (grey) — VLR's convention.
ROUND_WIN_GREEN = (44, 158, 122)
ROUND_LOSS_RED = (190, 68, 76)
ROUND_EMPTY_FILL = (56, 66, 84)  # rounds not played + rounds lost

# Per-stat accents (VLR uses subtle hues; we keep most white and color
# only the +/- deltas).
DELTA_POS_GREEN = (96, 220, 134)
DELTA_NEG_RED = (228, 110, 116)
DELTA_NEUTRAL = SOFT_GRAY


# ── Agent icon cache ─────────────────────────────────────────────
_AGENT_ICON_CACHE_MAXSIZE: int = 64
_AGENT_ICON_CACHE: OrderedDict[str, Image.Image] = OrderedDict()


def _agent_icon_cache_get(key: str) -> Image.Image | None:
    img = _AGENT_ICON_CACHE.get(key)
    if img is not None:
        _AGENT_ICON_CACHE.move_to_end(key)
    return img


def _agent_icon_cache_set(key: str, img: Image.Image) -> None:
    _AGENT_ICON_CACHE[key] = img
    _AGENT_ICON_CACHE.move_to_end(key)
    while len(_AGENT_ICON_CACHE) > _AGENT_ICON_CACHE_MAXSIZE:
        _AGENT_ICON_CACHE.popitem(last=False)


# ── Font / text helpers ──────────────────────────────────────────
def _font(size: int, bold: bool = True):
    bold_paths = [
        "C:/Windows/Fonts/Inter-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    regular_paths = [
        "C:/Windows/Fonts/Inter-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in bold_paths if bold else regular_paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])
    except AttributeError:
        return int(font.getsize(text)[0])


def _draw_v_center(draw, text, x_left, y_center, font, color):
    """Draw ``text`` with its vertical middle aligned to ``y_center``."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        y_arg = y_center - (bbox[1] + bbox[3]) // 2
    except AttributeError:
        _, h = font.getsize(text)
        y_arg = y_center - h // 2
    draw.text((x_left, y_arg), text, fill=color, font=font)


def _draw_centered(draw, text, x_center, y_center, font, color):
    w = _text_w(draw, text, font)
    _draw_v_center(draw, text, x_center - w // 2, y_center, font, color)


def _draw_right(draw, text, x_right, y_center, font, color):
    w = _text_w(draw, text, font)
    _draw_v_center(draw, text, x_right - w, y_center, font, color)


# ── Agent icon (local assets) ─────────────────────────────────────
_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
)
_AGENTS_DIR = os.path.join(_ASSETS_DIR, "agents")
_ROUND_OUTCOMES_DIR = os.path.join(_ASSETS_DIR, "round_outcomes")

# Map Henrik ``end_type`` strings to the on-disk icon basename. Any value
# not present here renders an empty square (winner colour only).
_ROUND_OUTCOME_FILES: dict[str, str] = {
    "Eliminated": "eliminated.png",
    "Bomb defused": "defused.png",
    "Bomb detonated": "detonated.png",
    "Round timer expired": "time.png",
    # Henrik sometimes uses shorthand — keep aliases for safety.
    "Time": "time.png",
    "Defused": "defused.png",
    "Detonated": "detonated.png",
}

_ROUND_ICON_CACHE: dict[tuple[str, int], Image.Image] = {}


def _load_round_outcome_icon(end_type: str, size: int) -> Image.Image | None:
    """Return a square RGBA icon for ``end_type`` resized to ``size``.

    Cached per (end_type, size). Returns ``None`` for unknown end types or
    when the asset is missing — the scoreboard then renders an empty
    square (winner colour only)."""
    filename = _ROUND_OUTCOME_FILES.get(end_type)
    if not filename:
        return None
    key = (filename, size)
    cached = _ROUND_ICON_CACHE.get(key)
    if cached is not None:
        return cached
    path = os.path.join(_ROUND_OUTCOMES_DIR, filename)
    if not os.path.isfile(path):
        return None
    try:
        img = Image.open(path).convert("RGBA")
        img = img.resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        return None
    _ROUND_ICON_CACHE[key] = img
    return img


def _agent_icon_filename(agent: str) -> str:
    """Map an agent name to its on-disk filename, normalizing the one
    edge case (``"KAY/O"`` → ``KAY_O.png``)."""
    safe = agent.replace("/", "_")
    return f"{safe}.png"


def _load_agent_icon(agent: str | None) -> Image.Image | None:
    """Returns the agent icon from local cache (memory or disk).
    None if the agent is unknown / file is missing."""
    if not agent:
        return None
    key = f"{agent}:{AGENT_ICON_SIZE}"
    cached = _agent_icon_cache_get(key)
    if cached is not None:
        return cached
    path = os.path.join(_AGENTS_DIR, _agent_icon_filename(agent))
    if not os.path.isfile(path):
        return None
    try:
        img = Image.open(path).convert("RGBA")
        img = img.resize((AGENT_ICON_SIZE, AGENT_ICON_SIZE), Image.Resampling.LANCZOS)
    except Exception:
        return None
    _agent_icon_cache_set(key, img)
    return img


def _placeholder_agent_icon() -> Image.Image:
    """Soft grey square used when the agent is unknown."""
    return Image.new("RGBA", (AGENT_ICON_SIZE, AGENT_ICON_SIZE), (*DIM_GRAY, 255))


# ── Public API ───────────────────────────────────────────────────
def generate_scoreboard(
    *,
    map_name: str,
    rounds_a: int,
    rounds_b: int,
    team_a_label: str,
    team_b_label: str,
    team_a_players: Iterable[Mapping[str, Any]],
    team_b_players: Iterable[Mapping[str, Any]],
    queue_label: str = "",
    round_winners: Sequence[str] = (),
    round_end_types: Sequence[str] = (),
) -> BytesIO:
    """Generate a VLR-style PNG scoreboard and return it as a ``BytesIO``."""
    a_players: Sequence[Mapping[str, Any]] = sorted(
        team_a_players, key=lambda p: _rating(p), reverse=True
    )
    b_players: Sequence[Mapping[str, Any]] = sorted(
        team_b_players, key=lambda p: _rating(p), reverse=True
    )

    height = (
        HEADER_BAND
        + ROUND_BAR_BAND
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + BLOCK_GAP
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + FOOTER_BAND
    )
    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    a_wins = rounds_a > rounds_b
    b_wins = rounds_b > rounds_a
    team_a_round_color = (
        ROUND_WIN_GREEN if a_wins else (ROUND_LOSS_RED if b_wins else ROUND_EMPTY_FILL)
    )
    team_b_round_color = (
        ROUND_WIN_GREEN if b_wins else (ROUND_LOSS_RED if a_wins else ROUND_EMPTY_FILL)
    )

    # 1) Header band
    _draw_header(
        draw,
        team_a_label=team_a_label,
        team_b_label=team_b_label,
        rounds_a=rounds_a,
        rounds_b=rounds_b,
        map_name=map_name,
        queue_label=queue_label,
    )

    # 2) Round bar (per-team W/L squares).
    round_bar_y = HEADER_BAND
    _draw_round_bar(
        img,
        draw,
        round_bar_y,
        round_winners,
        round_end_types,
        team_a_color=team_a_round_color,
        team_b_color=team_b_round_color,
    )

    # 3) Team A block.
    block_a_y = round_bar_y + ROUND_BAR_BAND
    _draw_team_block(img, draw, block_a_y, a_players, is_winner=rounds_a > rounds_b)

    # 4) Team B block.
    block_b_y = block_a_y + COLUMN_HEADER_BAND + PLAYERS_PER_TEAM * ROW_HEIGHT + BLOCK_GAP
    _draw_team_block(img, draw, block_b_y, b_players, is_winner=rounds_b > rounds_a)

    # 5) Footer.
    footer_y = height - FOOTER_BAND // 2
    _draw_centered(
        draw,
        "Play'IT Bot de Matchmaking",
        WIDTH // 2,
        footer_y,
        _font(14, bold=False),
        DIM_GRAY,
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ── Header ───────────────────────────────────────────────────────
def _draw_header(
    draw: ImageDraw.ImageDraw,
    *,
    team_a_label: str,
    team_b_label: str,
    rounds_a: int,
    rounds_b: int,
    map_name: str,
    queue_label: str,
) -> None:
    """Big header strip: labels + scores on each side, map (+ queue) centered."""
    draw.rectangle([(0, 0), (WIDTH, HEADER_BAND)], fill=HEADER_BG)

    y_center = HEADER_BAND // 2
    label_font = _font(26, bold=True)
    score_font = _font(54, bold=True)
    map_font = _font(24, bold=True)
    sub_font = _font(13, bold=True)

    a_wins = rounds_a > rounds_b
    b_wins = rounds_b > rounds_a
    a_color = WIN_GREEN if a_wins else (LOSE_RED if b_wins else WHITE)
    b_color = WIN_GREEN if b_wins else (LOSE_RED if a_wins else WHITE)

    # Left: team A label + score (aligned left after a small pad).
    label_a_x = PAD_LEFT
    _draw_v_center(draw, team_a_label.upper(), label_a_x, y_center, label_font, a_color)
    score_a_x = label_a_x + _text_w(draw, team_a_label.upper(), label_font) + 24
    _draw_v_center(draw, str(rounds_a), score_a_x, y_center, score_font, a_color)

    # Right: score B + label B (aligned right).
    label_b_x_right = WIDTH - PAD_LEFT
    label_b_w = _text_w(draw, team_b_label.upper(), label_font)
    _draw_v_center(
        draw,
        team_b_label.upper(),
        label_b_x_right - label_b_w,
        y_center,
        label_font,
        b_color,
    )
    score_b_x = label_b_x_right - label_b_w - 24
    score_b_w = _text_w(draw, str(rounds_b), score_font)
    _draw_v_center(draw, str(rounds_b), score_b_x - score_b_w, y_center, score_font, b_color)

    # Center: map name (queue label as small subtitle).
    if map_name:
        _draw_centered(draw, map_name, WIDTH // 2, y_center - 10, map_font, WHITE)
    if queue_label:
        _draw_centered(
            draw,
            queue_label.upper(),
            WIDTH // 2,
            y_center + 18,
            sub_font,
            HEADER_GRAY,
        )


# ── Round bar ────────────────────────────────────────────────────
_ROUND_BAR_SLOTS = 24  # match the VLR reference (12 + 12 with a half-time gap)


def _draw_round_bar(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y_top: int,
    round_winners: Sequence[str],
    round_end_types: Sequence[str],
    *,
    team_a_color: tuple[int, int, int],
    team_b_color: tuple[int, int, int],
) -> None:
    """Two horizontal strips of small squares: top row = team A, bottom
    row = team B. Team A is mapped to the "Red" Henrik side and team B
    to "Blue".

    Each team's WON squares are filled with the team colour (green if
    the team won the match, red if it lost). LOST and not-played rounds
    stay empty (grey) — VLR convention. The outcome icon is stamped
    only on won squares.
    """
    draw.rectangle([(0, y_top), (WIDTH, y_top + ROUND_BAR_BAND)], fill=BG)

    rounds_total = max(len(round_winners), _ROUND_BAR_SLOTS)
    available_w = WIDTH - 2 * PAD_LEFT
    # Half-time gap between rounds 12 and 13 (VLR convention).
    half_gap = 12
    half = rounds_total // 2
    # Solve for cell + 4px spacing — keep each square small (~22px).
    spacing = 4
    cell = max(
        14,
        (available_w - spacing * (rounds_total - 1) - half_gap) // max(rounds_total, 1),
    )
    cell = min(cell, 26)
    row_h = (ROUND_BAR_BAND - 8) // 2
    square_h = min(cell, row_h - 4)
    icon_size = max(8, square_h - 6)

    # Center the bar horizontally.
    bar_w = cell * rounds_total + spacing * (rounds_total - 1) + half_gap
    x0 = (WIDTH - bar_w) // 2
    y_a_top = y_top + (ROUND_BAR_BAND - 2 * square_h - 4) // 2
    y_b_top = y_a_top + square_h + 4

    for i in range(rounds_total):
        winner = round_winners[i] if i < len(round_winners) else ""
        end_type = round_end_types[i] if i < len(round_end_types) else ""
        x_cell = x0 + i * (cell + spacing) + (half_gap if i >= half else 0)

        # Team A row (Henrik's "Red" side).
        fill_a = team_a_color if winner == "Red" else ROUND_EMPTY_FILL
        draw.rectangle(
            [(x_cell, y_a_top), (x_cell + cell - 1, y_a_top + square_h)],
            fill=fill_a,
        )
        if winner == "Red":
            _stamp_round_icon(img, end_type, x_cell, y_a_top, cell, square_h, icon_size)

        # Team B row (Henrik's "Blue" side).
        fill_b = team_b_color if winner == "Blue" else ROUND_EMPTY_FILL
        draw.rectangle(
            [(x_cell, y_b_top), (x_cell + cell - 1, y_b_top + square_h)],
            fill=fill_b,
        )
        if winner == "Blue":
            _stamp_round_icon(img, end_type, x_cell, y_b_top, cell, square_h, icon_size)


def _stamp_round_icon(
    img: Image.Image,
    end_type: str,
    x_cell: int,
    y_cell: int,
    cell_w: int,
    cell_h: int,
    icon_size: int,
) -> None:
    """Paste the outcome icon centered inside the round square."""
    icon = _load_round_outcome_icon(end_type, icon_size)
    if icon is None:
        return
    icon_x = x_cell + (cell_w - icon_size) // 2
    icon_y = y_cell + (cell_h - icon_size) // 2
    img.paste(icon, (icon_x, icon_y), icon)


# ── Team block ───────────────────────────────────────────────────
def _draw_team_block(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    y_top: int,
    players: Sequence[Mapping[str, Any]],
    *,
    is_winner: bool,
) -> None:
    """Column header strip + 5 player rows for a single team."""
    accent = WIN_GREEN if is_winner else LOSE_RED

    _draw_block_header(draw, y_top)

    name_font = _font(18, bold=True)
    stats_font = _font(19, bold=True)
    kda_font = _font(17, bold=True)
    rows_y0 = y_top + COLUMN_HEADER_BAND
    for i in range(PLAYERS_PER_TEAM):
        y_row_top = rows_y0 + i * ROW_HEIGHT
        y_center = y_row_top + ROW_HEIGHT // 2
        row_bg = ROW_BG_A if i % 2 == 0 else ROW_BG_B
        draw.rectangle(
            [(0, y_row_top), (WIDTH, y_row_top + ROW_HEIGHT - 1)],
            fill=row_bg,
        )
        # Left accent bar (team color).
        draw.rectangle(
            [
                (PAD_LEFT, y_row_top + 8),
                (PAD_LEFT + ACCENT_W - 1, y_row_top + ROW_HEIGHT - 8),
            ],
            fill=accent,
        )
        if i < len(players):
            _draw_player_row(
                img,
                draw,
                players[i],
                y_center=y_center,
                name_font=name_font,
                stats_font=stats_font,
                kda_font=kda_font,
            )


def _draw_block_header(draw: ImageDraw.ImageDraw, y_top: int) -> None:
    """Column labels for one team block."""
    draw.rectangle(
        [(0, y_top), (WIDTH, y_top + COLUMN_HEADER_BAND)],
        fill=ROW_BG_HEADER,
    )
    draw.line(
        [(0, y_top + COLUMN_HEADER_BAND - 1), (WIDTH, y_top + COLUMN_HEADER_BAND - 1)],
        fill=SEPARATOR,
        width=1,
    )
    y_center = y_top + COLUMN_HEADER_BAND // 2
    font = _font(13, bold=True)
    # Player column left-aligned, others centered on the X_ constants.
    _draw_v_center(draw, "JOUEUR", NAME_X, y_center, font, HEADER_GRAY)
    for x, label in (
        (X_R, "R"),
        (X_ELO, "ELO +/-"),
        (X_ACS, "ACS"),
        (X_KDA, "K / D / A"),
        (X_KD, "+/-"),
        (X_KAST, "KAST"),
        (X_ADR, "ADR"),
        (X_HS, "HS%"),
        (X_FK, "FK"),
        (X_FD, "FD"),
        (X_FKFD, "+/-"),
    ):
        _draw_centered(draw, label, x, y_center, font, HEADER_GRAY)


def _draw_player_row(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    player: Mapping[str, Any],
    *,
    y_center: int,
    name_font,
    stats_font,
    kda_font,
) -> None:
    name = str(player.get("name", "?"))
    rating = _rating(player)
    acs = int(player.get("acs", 0) or 0)
    kills = int(player.get("kills", 0) or 0)
    deaths = int(player.get("deaths", 0) or 0)
    assists = int(player.get("assists", 0) or 0)
    kast_pct = float(player.get("kast_pct", 0.0) or 0.0)
    adr = float(player.get("adr", 0.0) or 0.0)
    hs_pct = float(player.get("hs_pct", 0.0) or 0.0)
    fk = int(player.get("first_kills", 0) or 0)
    fd = int(player.get("first_deaths", 0) or 0)
    agent = player.get("agent") or None

    # Name (truncated to leave room for the agent icon to its right).
    name_text = _truncate(draw, name, name_font, NAME_MAX_W)
    _draw_v_center(draw, name_text, NAME_X, y_center, name_font, WHITE)

    # Agent icon (small square right of the name).
    icon = _load_agent_icon(agent) or _placeholder_agent_icon()
    img.paste(
        icon,
        (AGENT_X, y_center - AGENT_ICON_SIZE // 2),
        icon if icon.mode == "RGBA" else None,
    )

    # Rating 2.0 — two decimals, colored by performance band.
    rating_color = _rating_color(rating)
    _draw_centered(draw, f"{rating:.2f}", X_R, y_center, stats_font, rating_color)

    # ELO gained/lost this match — green/red delta, right after the rating.
    elo_delta = int(player.get("elo_delta", 0) or 0)
    _draw_delta(draw, elo_delta, X_ELO, y_center, stats_font)

    # ACS — flat integer.
    _draw_centered(draw, str(acs), X_ACS, y_center, stats_font, WHITE)

    # K / D / A — slash-separated.
    _draw_centered(draw, f"{kills} / {deaths} / {assists}", X_KDA, y_center, kda_font, WHITE)

    # +/- kills - deaths.
    kd_diff = kills - deaths
    _draw_delta(draw, kd_diff, X_KD, y_center, stats_font)

    # KAST + ADR + HS% — neutral white.
    _draw_centered(draw, f"{round(kast_pct)}%", X_KAST, y_center, stats_font, WHITE)
    _draw_centered(draw, str(round(adr)), X_ADR, y_center, stats_font, WHITE)
    _draw_centered(draw, f"{round(hs_pct)}%", X_HS, y_center, stats_font, WHITE)

    # FK / FD / +-.
    _draw_centered(draw, str(fk), X_FK, y_center, stats_font, WHITE)
    _draw_centered(draw, str(fd), X_FD, y_center, stats_font, WHITE)
    _draw_delta(draw, fk - fd, X_FKFD, y_center, stats_font)


# ── Number formatting helpers ────────────────────────────────────
def _rating(player: Mapping[str, Any]) -> float:
    try:
        return float(player.get("rating_2_0", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rating_color(rating: float) -> tuple[int, int, int]:
    if rating >= 1.10:
        return DELTA_POS_GREEN
    if rating < 0.85:
        return DELTA_NEG_RED
    return WHITE


def _draw_delta(draw, value: int, x_center: int, y_center: int, font) -> None:
    """Centered ``+N`` / ``-N`` / ``0`` with green/red/grey accent."""
    if value > 0:
        text = f"+{value}"
        color = DELTA_POS_GREEN
    elif value < 0:
        text = str(value)
        color = DELTA_NEG_RED
    else:
        text = "0"
        color = DELTA_NEUTRAL
    _draw_centered(draw, text, x_center, y_center, font, color)


def _truncate(draw, text: str, font, max_width: int) -> str:
    if _text_w(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    truncated = text
    while truncated and _text_w(draw, truncated + ellipsis, font) > max_width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis
