"""Pure tests for the Pro Queue captain draft."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from services.captain_draft import PICK_SEQUENCE, DraftResult, DraftState, _is_admin, pick_captains
from services.match_service import build_plan_from_draft
from services.team_balancer import Player

pytestmark = pytest.mark.unit


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def test_pick_captains_returns_two_distinct_players_from_pool():
    """The captains are 2 distinct players drawn from the list, regardless of ELO."""
    players = [
        _p(1, 1000),
        _p(2, 1100),
        _p(3, 1200),
        _p(4, 1300),
        _p(5, 1400),
        _p(6, 1500),
        _p(7, 1600),
        _p(8, 1700),
        _p(9, 1800),
        _p(10, 1900),
    ]
    rng = random.Random(42)
    cap_a, cap_b = pick_captains(players, rng=rng)
    ids = {p.id for p in players}
    assert cap_a.id != cap_b.id
    assert cap_a.id in ids
    assert cap_b.id in ids


def test_pick_captains_is_reproducible_with_same_seed():
    """Same seed -> same captain pair."""
    players = [_p(i, 1000 + i * 50) for i in range(1, 11)]
    cap_a_1, cap_b_1 = pick_captains(players, rng=random.Random(1))
    cap_a_2, cap_b_2 = pick_captains(players, rng=random.Random(1))
    assert (cap_a_1.id, cap_b_1.id) == (cap_a_2.id, cap_b_2.id)


def test_pick_captains_ignores_elo():
    """The top ELO player is NOT guaranteed to be a captain (purely random selection).

    We verify that on at least some seeds, the top ELO player is not picked
    as a captain - proof that ELO no longer influences the selection.
    """
    players = [_p(1, 5000)] + [_p(i, 1000) for i in range(2, 11)]
    top_elo_id = 1
    seen_without_top = False
    for seed in range(50):
        cap_a, cap_b = pick_captains(players, rng=random.Random(seed))
        if top_elo_id not in (cap_a.id, cap_b.id):
            seen_without_top = True
            break
    assert seen_without_top, "The top ELO player should be able to not be a captain"


def test_pick_captains_raises_if_too_few_players():
    with pytest.raises(ValueError, match="At least 2 players"):
        pick_captains([_p(1, 1500)], rng=random.Random(0))


def test_pick_captains_raises_if_empty():
    with pytest.raises(ValueError, match="At least 2 players"):
        pick_captains([], rng=random.Random(0))


def test_pick_sequence_is_snake_ABBAABBA():
    assert PICK_SEQUENCE == ("A", "B", "B", "A", "A", "B", "B", "A")


def test_draft_state_initial():
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
    assert state.team_a == (cap_a,)
    assert state.team_b == (cap_b,)
    assert state.pool == pool
    assert state.turn_index == 0
    assert state.status == "picking"
    assert state.current_captain is cap_a  # PICK_SEQUENCE[0] == "A"
    assert not state.is_complete


def _make_state_with_8_pool() -> tuple[DraftState, list[Player]]:
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = [_p(i, 1500 - i) for i in range(3, 11)]  # 8 players
    return DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=tuple(pool)), pool


def test_draft_state_apply_pick_is_immutable():
    state, pool = _make_state_with_8_pool()
    state2 = state.apply_pick(pool[0])
    # original inchange
    assert state.team_a == (state.cap_a,)
    assert state.pool == tuple(pool)
    assert state.turn_index == 0
    # new shifted state
    assert state2.team_a == (state.cap_a, pool[0])
    assert pool[0] not in state2.pool
    assert state2.turn_index == 1


def test_draft_state_apply_pick_follows_ABBAABBA():
    state, pool = _make_state_with_8_pool()
    expected_sides = ["A", "B", "B", "A", "A", "B", "B", "A"]
    for i, side in enumerate(expected_sides):
        assert state.current_captain.id == (state.cap_a.id if side == "A" else state.cap_b.id), (
            f"turn {i}: expected side {side}"
        )
        state = state.apply_pick(pool[i])
    assert state.is_complete
    assert state.status == "complete"


def test_draft_state_complete_has_5_each_team():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    assert len(state.team_a) == 5
    assert len(state.team_b) == 5
    assert state.pool == ()


def test_draft_state_apply_pick_rejects_player_not_in_pool():
    state, _ = _make_state_with_8_pool()
    stranger = _p(99, 1500)
    with pytest.raises(ValueError, match="not in the pool"):
        state.apply_pick(stranger)


def test_draft_state_apply_pick_rejects_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    extra = _p(99, 1500)
    with pytest.raises(RuntimeError, match="status=complete"):
        state.apply_pick(extra)


def test_draft_result_from_state_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    assert result.cap_a is state.cap_a
    assert result.cap_b is state.cap_b
    assert len(result.team_a) == 5 and len(result.team_b) == 5


def test_draft_result_rejects_incomplete_state():
    state, _ = _make_state_with_8_pool()
    with pytest.raises(ValueError, match="not complete"):
        DraftResult.from_state(state)


ADMIN_ROLE_NAMES = ("Admin", "Match Staff", "Administrateur")


def _fake_user(*, role_names: tuple[str, ...] = (), manage_guild: bool = False):
    """Mock a discord.Member for `_is_admin`: `roles` + `guild_permissions`."""
    return SimpleNamespace(
        roles=[SimpleNamespace(name=n) for n in role_names],
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
    )


def test_is_admin_accepts_manage_guild_permission():
    """A Discord admin (manage_guild=True) must be able to cancel the
    draft, even without a role named 'Admin'/'Match Staff'/'Administrator'."""
    user = _fake_user(role_names=("Administrator",), manage_guild=True)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is True


def test_is_admin_accepts_named_admin_role_as_fallback():
    """Backwards-compat: a user with the 'Match Staff' role but no
    manage_guild permission stays authorized (staff without elevated perms)."""
    user = _fake_user(role_names=("Match Staff",), manage_guild=False)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is True


def test_is_admin_rejects_regular_user():
    user = _fake_user(role_names=("Member",), manage_guild=False)
    assert _is_admin(user, ADMIN_ROLE_NAMES) is False


def test_is_admin_handles_missing_attributes():
    """Robustness: an object without `guild_permissions` or `roles` does not crash."""
    bare = SimpleNamespace()
    assert _is_admin(bare, ADMIN_ROLE_NAMES) is False


def test_build_plan_from_draft_uses_capA_as_leader():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(42),
    )
    assert plan.category_name == "Match #1"
    assert plan.lobby_leader is state.cap_a
    assert plan.teams.team_a == result.team_a
    assert plan.teams.team_b == result.team_b
    # map_name is chosen by rng from elo_calc.MAPS, non-empty
    assert plan.map_name
