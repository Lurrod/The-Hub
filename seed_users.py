"""
Generates N fake players in MongoDB to test the bot with real data.

Useful for testing /leaderboard, /stats, /resetelo etc. in Discord
with a well-filled ranking, without having to create 30 real players.

Requirements:
    pip install faker pymongo

Environment variables:
    MONGO_URL       (default: mongodb://localhost:27017)
    TEST_GUILD_ID   (required: the ID of your test Discord server)
    N_USERS         (default: 30)

Usage:
    set TEST_GUILD_ID=123456789012345678
    python seed_users.py

    # To reset the fake players afterwards:
    python seed_users.py --clean
"""

import os
import sys
import random

try:
    from pymongo import MongoClient
    from faker import Faker
except ImportError:
    print("[ERROR] Install dependencies: pip install pymongo faker")
    sys.exit(1)

MONGO_URL     = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
TEST_GUILD_ID = os.environ.get("TEST_GUILD_ID")
N_USERS       = int(os.environ.get("N_USERS", "30"))

if not TEST_GUILD_ID:
    print("[ERROR] Set the TEST_GUILD_ID variable (ID of your test Discord server).")
    print("        ex: set TEST_GUILD_ID=123456789012345678")
    sys.exit(1)

# Fake Discord IDs: we use snowflakes from a base reserved for tests
# (real Discord IDs have ~18 digits, we prefix with 9999 to distinguish them)
FAKE_ID_PREFIX = 9999

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
try:
    client.admin.command("ping")
except Exception as e:
    print(f"[ERROR] MongoDB unreachable at {MONGO_URL}: {e}")
    sys.exit(1)

db  = client["elobot"]
col = db[f"elo_{TEST_GUILD_ID}"]

# --clean mode: deletes only the fake players (preserves the real ones)
if "--clean" in sys.argv:
    res = col.delete_many({"_id": {"$regex": f"^{FAKE_ID_PREFIX}"}})
    print(f"[ok] {res.deleted_count} fake players deleted from elo_{TEST_GUILD_ID}")
    sys.exit(0)

# ── Generation ────────────────────────────────────────────────────
fake = Faker(["fr_FR", "en_US"])
random.seed(42)

inserted = 0
for i in range(N_USERS):
    fake_id = f"{FAKE_ID_PREFIX}{i:014d}"  # ex: 9999000000000000000
    wins   = random.randint(0, 50)
    losses = random.randint(0, 50)
    kills  = random.randint(wins * 5, wins * 25 + losses * 10)
    deaths = random.randint(losses * 5, losses * 20 + wins * 8)
    elo    = max(0, wins * 17 - losses * 12 + random.randint(-30, 30))
    col.update_one(
        {"_id": fake_id},
        {"$set": {
            "name":   fake.user_name()[:20],
            "elo":    elo,
            "wins":   wins,
            "losses": losses,
            "kills":  kills,
            "deaths": deaths,
        }},
        upsert=True,
    )
    inserted += 1

print(f"[ok] {inserted} fake players inserted into elo_{TEST_GUILD_ID}")
print(f"     /leaderboard should display at least {inserted} entries.")
print(f"     To clean them up later: python seed_users.py --clean")
print()
print("[!] Avatars will not display in Discord (fake IDs).")
print("    For a complete visual test, use preview_leaderboard.py instead")
