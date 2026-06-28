"""Pure tests for the map pick/ban state machine."""

from __future__ import annotations

import pytest

from services.map_pick_ban import (
    BAN_SEQUENCE,
    MapBanState,
)
from services.team_balancer import Player

pytestmark = pytest.mark.unit


def _p(uid: int, elo: int = 2000) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


MAPS_7 = ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")


def test_ban_sequence_is_ABABAB():
    assert BAN_SEQUENCE == ("A", "B", "A", "B", "A", "B")


def test_initial_state():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    assert state.cap_a.id == 1
    assert state.cap_b.id == 2
    assert state.remaining == MAPS_7
    assert state.banned == ()
    assert state.turn_index == 0
    assert state.status == "banning"
    assert not state.is_complete


def test_current_captain_alternates():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    assert state.current_captain.id == 1  # turn 0 = A
    state2 = state.apply_ban("Breeze")
    assert state2.current_captain.id == 2  # turn 1 = B
    state3 = state2.apply_ban("Ascent")
    assert state3.current_captain.id == 1  # turn 2 = A


from dataclasses import replace


def test_apply_ban_removes_map_and_records_history():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state2 = state.apply_ban("Breeze")
    assert "Breeze" not in state2.remaining
    assert state2.banned == (("A", "Breeze"),)
    assert state2.turn_index == 1
    assert state2.status == "banning"


def test_apply_ban_is_immutable():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state.apply_ban("Breeze")
    # original unchanged
    assert state.remaining == MAPS_7
    assert state.banned == ()
    assert state.turn_index == 0


def test_apply_ban_raises_on_unknown_map():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    with pytest.raises(ValueError, match="not in remaining"):
        state.apply_ban("Sunset")


def test_apply_ban_raises_on_already_banned_map():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state2 = state.apply_ban("Breeze")
    with pytest.raises(ValueError, match="not in remaining"):
        state2.apply_ban("Breeze")


def test_apply_ban_raises_when_cancelled():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    cancelled = replace(state, status="cancelled")
    with pytest.raises(RuntimeError, match="cannot ban"):
        cancelled.apply_ban("Breeze")


def _ban_all_six(cap_a: Player, cap_b: Player) -> MapBanState:
    state = MapBanState.initial(cap_a=cap_a, cap_b=cap_b, maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    return state


def test_six_bans_leave_one_map_and_move_to_side_pick():
    state = _ban_all_six(_p(1), _p(2))
    assert state.status == "picking_side"
    assert state.bans_done
    assert not state.is_complete  # side not picked yet
    assert state.remaining == ("Pearl",)
    assert len(state.banned) == 6


def test_apply_ban_raises_once_bans_done():
    state = _ban_all_six(_p(1), _p(2))
    with pytest.raises(RuntimeError, match="cannot ban"):
        state.apply_ban("Pearl")


def test_current_captain_raises_when_bans_done():
    state = _ban_all_six(_p(1), _p(2))
    with pytest.raises(RuntimeError, match="no current captain"):
        _ = state.current_captain


def test_side_captain_is_the_one_who_did_not_make_final_ban():
    # BAN_SEQUENCE ends on "B" (cap_b), so cap_a picks the side.
    state = _ban_all_six(_p(1), _p(2))
    assert state.side_captain.id == 1


def test_apply_side_completes_and_records_side():
    state = _ban_all_six(_p(1), _p(2))
    completed = state.apply_side("Attack")
    assert completed.status == "complete"
    assert completed.is_complete
    assert completed.picked_side == "Attack"
    # original unchanged (immutability)
    assert state.picked_side is None
    assert state.status == "picking_side"


def test_apply_side_raises_when_not_picking_side():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    with pytest.raises(RuntimeError, match="cannot pick side"):
        state.apply_side("Attack")


def test_apply_side_raises_on_invalid_side():
    state = _ban_all_six(_p(1), _p(2))
    with pytest.raises(ValueError, match="Invalid side"):
        state.apply_side("Offense")  # type: ignore[arg-type]


def test_map_ban_result_from_complete_state():
    from services.map_pick_ban import MapBanResult

    state = _ban_all_six(_p(1), _p(2)).apply_side("Defense")
    result = MapBanResult.from_state(state)
    assert result.selected_map == "Pearl"
    assert result.picked_side == "Defense"
    assert result.side_captain_id == 1
    assert result.ban_history == (
        ("A", "Breeze"),
        ("B", "Ascent"),
        ("A", "Lotus"),
        ("B", "Fracture"),
        ("A", "Split"),
        ("B", "Haven"),
    )


def test_map_ban_result_raises_if_bans_not_done():
    from services.map_pick_ban import MapBanResult

    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    with pytest.raises(ValueError, match="not complete"):
        MapBanResult.from_state(state)


def test_map_ban_result_raises_if_side_not_picked():
    from services.map_pick_ban import MapBanResult

    state = _ban_all_six(_p(1), _p(2))  # picking_side, no side yet
    with pytest.raises(ValueError, match="not complete"):
        MapBanResult.from_state(state)


def test_map_ban_cancelled_error_stores_reason_and_actor():
    from services.map_pick_ban import MapBanCancelledError

    class _FakeActor:
        id = 42

    actor = _FakeActor()
    err = MapBanCancelledError("admin", actor=actor)
    assert err.reason == "admin"
    assert err.actor is actor


def test_map_ban_cancelled_error_actor_defaults_to_none():
    from services.map_pick_ban import MapBanCancelledError

    err = MapBanCancelledError("system")
    assert err.reason == "system"
    assert err.actor is None
