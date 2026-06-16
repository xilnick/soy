"""
soy.state_machine
=================

Mission lifecycle state machine.

The state machine is implemented as a pure-Python data structure
(``MissionStateMachine``) so that the same rules can be enforced by
the API router, the PraisonAI worker, and the WebSocket layer. The
allowed transitions are listed in :data:`ALLOWED_TRANSITIONS`.

The state machine does not touch the database — the router
(``soy.api.v1.missions``) loads the row, asks the machine whether a
transition is allowed, and applies the change inside a single
``SELECT ... FOR UPDATE`` transaction. This keeps the unit tests
fast (no DB) and makes the rules easy to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional

from soy.models.enums import MissionStatus


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------
# Each entry maps the *current* status to the set of statuses a
# mission can be moved to. The list is intentionally explicit (rather
# than computed from a list of "happy-path" edges) so that adding a
# new status cannot accidentally widen the set of allowed transitions.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: Dict[MissionStatus, FrozenSet[MissionStatus]] = {
    MissionStatus.created: frozenset({MissionStatus.planning}),
    MissionStatus.planning: frozenset(
        {
            MissionStatus.approved,
            MissionStatus.rejected,
            MissionStatus.execution,  # direct approval & skip
        }
    ),
    MissionStatus.approved: frozenset({MissionStatus.execution}),
    MissionStatus.execution: frozenset(
        {MissionStatus.reviewed, MissionStatus.rejected, MissionStatus.escalated}
    ),
    MissionStatus.reviewed: frozenset(
        {MissionStatus.merged, MissionStatus.rejected, MissionStatus.escalated}
    ),
    # A rejected mission normally bounces back to ``planning`` for
    # rework; after too many rejections the reject endpoint escalates
    # it (``rejected -> escalated``), so that edge is legal too.
    MissionStatus.rejected: frozenset(
        {MissionStatus.planning, MissionStatus.escalated}
    ),
    MissionStatus.merged: frozenset(),  # terminal
    MissionStatus.escalated: frozenset(),  # terminal until manual reset
}


# When a rejection is recorded for a mission, this counter is bumped
# in the mission's ``metadata`` JSONB column. The fourth rejection
# (i.e. the counter reaching 3 → 4) is the trigger for the
# ``escalated`` state.
ESCALATION_REJECTION_THRESHOLD = 4


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of asking the state machine about a transition.

    ``allowed`` is True when the transition is permitted by the
    static rules. ``reason`` is a machine-readable string for the
    structured error code; ``allowed_list`` is the set of statuses
    the caller could transition to from the current state.
    """

    allowed: bool
    reason: str = ""
    allowed_list: Optional[List[MissionStatus]] = None


class MissionStateMachine:
    """Mission lifecycle state machine.

    Usage::

        machine = MissionStateMachine()
        result = machine.can_transition(MissionStatus.created, MissionStatus.planning)
        if result.allowed:
            # apply the change
            ...

    The machine is stateless; the methods are pure functions of their
    arguments, so it is safe to share a single instance across
    requests.
    """

    def allowed_targets(self, current: MissionStatus) -> List[MissionStatus]:
        """Return the sorted list of statuses reachable from ``current``.

        The list is sorted (by the enum definition order) so the API
        response is deterministic — important for the structured
        error code the validation contract requires.
        """
        return sorted(ALLOWED_TRANSITIONS.get(current, frozenset()), key=lambda s: s.value)

    def can_transition(
        self, current: MissionStatus, target: MissionStatus
    ) -> TransitionResult:
        """Return whether ``current`` may move to ``target``."""
        if current == target:
            return TransitionResult(
                allowed=False,
                reason="no_op_transition",
                allowed_list=self.allowed_targets(current),
            )
        if target not in self.allowed_targets(current):
            return TransitionResult(
                allowed=False,
                reason="invalid_transition",
                allowed_list=self.allowed_targets(current),
            )
        return TransitionResult(allowed=True)

    def should_escalate(self, rejection_count: int) -> bool:
        """Return True when the rejection count triggers escalation.

        The check is ``count >= threshold`` so the API can evaluate
        the rule *after* incrementing the counter. The first three
        rejections return False (count 1, 2, 3), and the fourth
        rejection (count == 4) returns True.
        """
        return rejection_count >= ESCALATION_REJECTION_THRESHOLD


# A module-level singleton — stateless, safe to share.
mission_state_machine = MissionStateMachine()
