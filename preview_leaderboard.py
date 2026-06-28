"""
Visual leaderboard test WITHOUT Discord.

Generates N fake players and renders each leaderboard page as a PNG in ./leaderboard_preview/.
If the PNG rendering crashes on page 2+, the bug is in generate_leaderboard,
not in the Discord pagination.

Requirements:
    pip install faker pillow requests

Usage:
    python preview_leaderboard.py            # 30 players (2 pages)
    python preview_leaderboard.py 16         # 16 players (2 pages, last page = 1 player)
    python preview_leaderboard.py 100        # 100 players (7 pages)
    python preview_leaderboard.py 15         # 15 players (1 page)
"""

import os
import sys
import random
from pathlib import Path

try:
    from faker import Faker
except ImportError:
    print("[ERROR] Install faker: pip install faker")
    sys.exit(1)

try:
    from leaderboard_img import generate_leaderboard
except ImportError:
    print("[ERROR] leaderboard_img.py not found.")
    print("Place it in the same folder as this script or in the PYTHONPATH.")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────
PAGE_SIZE = 15
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
OUT_DIR = Path(__file__).parent / "leaderboard_preview"
OUT_DIR.mkdir(exist_ok=True)

# Default Discord avatars (public, no login needed)
DEFAULT_AVATARS = [
    f"https://cdn.discordapp.com/embed/avatars/{i}.png" for i in range(6)
]

# ── Fake player generation ────────────────────────────────────────
fake = Faker(["fr_FR", "en_US"])
random.seed(42)  # reproducible

players = []
for _ in range(N):
    wins   = random.randint(0, 50)
    losses = random.randint(0, 50)
    kills  = random.randint(wins * 5, wins * 25 + losses * 10)
    deaths = random.randint(losses * 5, losses * 20 + wins * 8)
    elo    = max(0, wins * 17 - losses * 12 + random.randint(-30, 30))
    players.append({
        "name":       fake.user_name()[:20],
        "elo":        elo,
        "wins":       wins,
        "losses":     losses,
        "kills":      kills,
        "deaths":     deaths,
        "avatar_url": random.choice(DEFAULT_AVATARS),
    })

# Sort by descending ELO + rank assignment (same as in the bot)
players.sort(key=lambda p: -p["elo"])
for rank, p in enumerate(players, start=1):
    p["rank"] = rank

# ── Render each page ──────────────────────────────────────────────
total_pages = max(1, (N + PAGE_SIZE - 1) // PAGE_SIZE)
print(f"[info] {N} players -> {total_pages} page(s) of {PAGE_SIZE} max")
print(f"[info] Output: {OUT_DIR}\n")

errors = 0
for page in range(total_pages):
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    out_path = OUT_DIR / f"page_{page + 1}.png"
    try:
        buf = generate_leaderboard(chunk, server_name="Test Server")
        if hasattr(buf, "read"):
            buf.seek(0)
            out_path.write_bytes(buf.read())
        elif isinstance(buf, (bytes, bytearray)):
            out_path.write_bytes(bytes(buf))
        elif isinstance(buf, str) and os.path.isfile(buf):
            out_path.write_bytes(Path(buf).read_bytes())
        else:
            print(f"  [warn] Page {page + 1}: unknown type ({type(buf).__name__})")
            errors += 1
            continue
        size_kb = out_path.stat().st_size // 1024
        print(f"  [ok]   Page {page + 1}/{total_pages} -> {out_path.name} "
              f"({len(chunk)} players, {size_kb} KB)")
    except Exception as e:
        import traceback
        print(f"  [FAIL] Page {page + 1}: {type(e).__name__}: {e}")
        traceback.print_exc()
        errors += 1

print()
if errors:
    print(f"[!] {errors} page(s) with errors. This is probably the bug you're looking for.")
    sys.exit(1)
else:
    print(f"[ok] {total_pages} page(s) rendered. Open them to verify visually.")
