"""Package cogs.match: formation + vote + Henrik verification + cleanups.

The module is split into private sub-modules (_constants, _embeds,
_vote, _cog); this __init__ re-exports the public API to preserve
existing imports (`from cogs.match import setup`, `MatchCog`,
`VoteView`, `build_match_embed`, etc.).
"""

from cogs.match._cog import MatchCog, setup
from cogs.match._constants import (
    ADMIN_ROLE_NAMES,
    HENRIK_CIRCUIT_FAIL_THRESHOLD,
    HENRIK_CIRCUIT_OPEN_MINUTES,
    HENRIK_VERIFY_DELAY_MINUTES,
    HENRIK_VERIFY_TIMEOUT_MINUTES,
    MAJORITY_THRESHOLD,
    MATCH_HOST_ROLE_NAME,
    MATCH_SPECTATOR_ROLE_NAMES,
    MATCH_VIEWER_ROLE_NAMES,
    MAX_REPLACE_ELO_DIFF,
    RESULTS_CHANNELS,
    VOTE_A_BTN_ID,
    VOTE_B_BTN_ID,
    VOTE_TIMEOUT_MINUTES,
)
from cogs.match._embeds import (
    build_elo_changes_embed,
    build_match_embed,
    build_match_embed_from_doc,
)
from cogs.match._vote import VoteView

__all__ = [
    "ADMIN_ROLE_NAMES",
    "HENRIK_CIRCUIT_FAIL_THRESHOLD",
    "HENRIK_CIRCUIT_OPEN_MINUTES",
    "HENRIK_VERIFY_DELAY_MINUTES",
    "HENRIK_VERIFY_TIMEOUT_MINUTES",
    "MAJORITY_THRESHOLD",
    "MATCH_HOST_ROLE_NAME",
    "MATCH_SPECTATOR_ROLE_NAMES",
    "MATCH_VIEWER_ROLE_NAMES",
    "MAX_REPLACE_ELO_DIFF",
    "RESULTS_CHANNELS",
    "VOTE_A_BTN_ID",
    "VOTE_B_BTN_ID",
    "VOTE_TIMEOUT_MINUTES",
    "MatchCog",
    "VoteView",
    "build_elo_changes_embed",
    "build_match_embed",
    "build_match_embed_from_doc",
    "setup",
]
