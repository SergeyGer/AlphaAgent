"""Typed errors for the Phase 1 decision-run domain.

Why a module of their own
-------------------------
``services/decision_runs.py`` and ``services/evidence.py`` enforce rules that a
caller can get wrong for an ordinary reason - an illegal status jump, a claim
citing evidence published after the analysis cutoff, a rebuttal aimed at its own
side. Those are *expected* refusals, not bugs, and a caller has to be able to
tell them apart from a ``TypeError`` or a driver error without parsing a message.

They live in ``core`` rather than in a service module because the models raise
one of them directly (``InputSnapshot.save`` refuses to rewrite a captured
snapshot) and models must not import services.

Every message is written to be shown to a reviewer as-is: the API maps these to
400 with the message in ``detail``, so "evidence ev-1 is not available at the
analysis cutoff" is the text a client sees, not a traceback.
"""

from __future__ import annotations

__all__ = [
    "ClaimValidationError",
    "DecisionError",
    "DecisionRunError",
    "EvidenceNotFound",
    "EvidenceValidationError",
    "IllegalTransition",
    "InvalidRunState",
    "RebuttalValidationError",
    "SnapshotImmutableError",
]


class DecisionError(Exception):
    """Base class for every expected refusal in the decision-run domain."""


class DecisionRunError(DecisionError):
    """A problem with a run's own lifecycle."""


class IllegalTransition(DecisionRunError):
    """A status change that the protocol does not allow."""


class InvalidRunState(DecisionRunError):
    """The run is in a state that cannot satisfy the requested operation."""


class SnapshotImmutableError(DecisionError):
    """An input snapshot was modified after capture.

    Raised by ``InputSnapshot.save``. A snapshot is the record of what the agents
    could see when they decided; rewriting it would let a decision be re-explained
    against data it never had, which is the one failure mode an audit trail must
    not permit.
    """


class EvidenceError(DecisionError):
    """Base class for evidence, claim and rebuttal validation failures."""


class EvidenceNotFound(EvidenceError):
    """A referenced evidence item or claim does not exist."""


class EvidenceValidationError(EvidenceError):
    """An evidence item violates the EVID-01 contract."""


class ClaimValidationError(EvidenceError):
    """A claim violates the EVID-02 contract (typically temporal availability)."""


class RebuttalValidationError(EvidenceError):
    """A rebuttal violates the EVID-03 contract (typically the wrong side)."""
