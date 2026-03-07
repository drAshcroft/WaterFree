"""
TurnManager — enforces the 8-state AI state machine.

The machine ensures the AI can only act within its designated turn,
and that file edits are only possible in the 'executing' state.

Valid transitions:
  idle           → planning, answering
  planning       → idle, annotating
  annotating     → awaiting_review
  awaiting_review→ executing, annotating, awaiting_redirect
  executing      → scanning
  scanning       → idle, annotating
  answering      → idle, awaiting_redirect
  awaiting_redirect → planning, annotating, idle
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from backend.session.models import AIState, PlanDocument

if TYPE_CHECKING:
    from backend.session.session_manager import SessionManager

log = logging.getLogger(__name__)

_VALID_TRANSITIONS: dict[AIState, list[AIState]] = {
    AIState.IDLE:              [AIState.PLANNING, AIState.ANSWERING],
    AIState.PLANNING:          [AIState.IDLE, AIState.ANNOTATING],
    AIState.ANNOTATING:        [AIState.AWAITING_REVIEW],
    AIState.AWAITING_REVIEW:   [AIState.EXECUTING, AIState.ANNOTATING, AIState.AWAITING_REDIRECT],
    AIState.EXECUTING:         [AIState.SCANNING],
    AIState.SCANNING:          [AIState.IDLE, AIState.ANNOTATING],
    AIState.ANSWERING:         [AIState.IDLE, AIState.AWAITING_REDIRECT],
    AIState.AWAITING_REDIRECT: [AIState.PLANNING, AIState.ANNOTATING, AIState.IDLE],
}


class TurnManager:
    def __init__(self, doc: PlanDocument, session_manager: "SessionManager"):
        self._doc = doc
        self._sm = session_manager

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def state(self) -> AIState:
        return self._doc.ai_state

    def can_edit_files(self) -> bool:
        return self._doc.ai_state == AIState.EXECUTING

    def can_annotate(self) -> bool:
        return self._doc.ai_state == AIState.ANNOTATING

    def can_plan(self) -> bool:
        return self._doc.ai_state == AIState.PLANNING

    def is_awaiting_review(self) -> bool:
        return self._doc.ai_state == AIState.AWAITING_REVIEW

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(self, new_state: AIState, save: bool = True) -> None:
        valid = _VALID_TRANSITIONS.get(self._doc.ai_state, [])
        if new_state not in valid:
            raise InvalidTransitionError(
                f"Cannot transition from {self._doc.ai_state.value!r} "
                f"to {new_state.value!r}. "
                f"Valid targets: {[s.value for s in valid]}"
            )
        log.debug("State: %s → %s", self._doc.ai_state.value, new_state.value)
        self._doc.ai_state = new_state
        if save:
            self._sm.save_session(self._doc)

    def force(self, new_state: AIState) -> None:
        """Override without validation — for error recovery only."""
        log.warning(
            "Forcing state to %s (was %s)", new_state.value, self._doc.ai_state.value
        )
        self._doc.ai_state = new_state
        self._sm.save_session(self._doc)

    # ------------------------------------------------------------------
    # Convenience transition methods (named for readability in callers)
    # ------------------------------------------------------------------

    def start_planning(self) -> None:
        self.transition(AIState.PLANNING)

    def start_annotating(self) -> None:
        if self._doc.ai_state == AIState.IDLE:
            self.transition(AIState.PLANNING, save=False)
        self.transition(AIState.ANNOTATING)

    def annotation_ready(self) -> None:
        self.transition(AIState.AWAITING_REVIEW)

    def begin_execution(self) -> None:
        self.transition(AIState.EXECUTING)

    def begin_scan(self) -> None:
        self.transition(AIState.SCANNING)

    def finish(self) -> None:
        self.transition(AIState.IDLE)

    def await_redirect(self) -> None:
        self.transition(AIState.AWAITING_REDIRECT)

    def start_answering(self) -> None:
        self.transition(AIState.ANSWERING)


class InvalidTransitionError(Exception):
    pass
