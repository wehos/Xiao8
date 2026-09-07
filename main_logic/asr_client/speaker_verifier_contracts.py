"""Provider-neutral installation identity, authority and ownership contracts.

No application/service imports belong here. Profile identity is deliberately
independent from activation, installation and health revisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Callable


class SpeakerVerifierAuthorityState(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    REVOKED = "revoked"


class SpeakerVerifierAuthority:
    """Synchronous, irreversible permission retirement shared by an activation."""

    def __init__(self) -> None:
        self._state = SpeakerVerifierAuthorityState.STAGED
        self._lock = Lock()

    @property
    def state(self) -> SpeakerVerifierAuthorityState:
        with self._lock:
            return self._state

    @property
    def permits_evidence(self) -> bool:
        return self.state is SpeakerVerifierAuthorityState.COMMITTED

    def commit(self) -> bool:
        with self._lock:
            if self._state is SpeakerVerifierAuthorityState.REVOKED:
                return False
            self._state = SpeakerVerifierAuthorityState.COMMITTED
            return True

    def revoke(self) -> None:
        with self._lock:
            self._state = SpeakerVerifierAuthorityState.REVOKED


class SpeakerVerifierInstallOutcome(str, Enum):
    INSTALLED = "installed"
    DEFERRED_ROUTE = "deferred_route"
    UNSUPPORTED_ROUTE = "unsupported_route"
    STALE = "stale"
    FAILED = "failed"
    REVOKED = "revoked"


class SpeakerVerifierOwnershipState(str, Enum):
    UNCREATED = "uncreated"
    OPERATION = "operation"
    DETECTOR = "detector"
    RETIRED = "retired"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SpeakerVerifierInstallIdentity:
    manager_identity: int
    runtime_identity: int
    session_generation: int
    route_generation: int
    detector_identity: int
    detector_epoch: int
    activation_revision: str
    installation_id: str


@dataclass(frozen=True, slots=True)
class SpeakerVerifierSpec:
    profile_generation: str | None
    activation_revision: str
    requested_enabled: bool
    enforce: bool
    revocable_authority: SpeakerVerifierAuthority
    # Called only when a supported live route exists, never on idle registration.
    factory_builder: Callable[[Any, SpeakerVerifierInstallIdentity], Any] | None


@dataclass(frozen=True, slots=True)
class SpeakerVerifierInstallReceipt:
    identity: SpeakerVerifierInstallIdentity | None
    outcome: SpeakerVerifierInstallOutcome
    ownership_state: SpeakerVerifierOwnershipState = (
        SpeakerVerifierOwnershipState.UNCREATED
    )
    health_revision: int = 0
    cleanup_pending: bool = False

    def __bool__(self) -> bool:
        raise TypeError("match SpeakerVerifierInstallReceipt.outcome explicitly")


@dataclass(frozen=True, slots=True)
class SpeakerVerifierHealthEvent:
    identity: SpeakerVerifierInstallIdentity
    health_revision: int
    causes: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class SpeakerVerifierReplacementOperation:
    """Queryable handoff; acceptance is written before Detector's first await."""

    identity: SpeakerVerifierInstallIdentity
    ownership_state: SpeakerVerifierOwnershipState = (
        SpeakerVerifierOwnershipState.OPERATION
    )
    outcome: SpeakerVerifierInstallOutcome = SpeakerVerifierInstallOutcome.STALE
    cleanup_pending: bool = False
    cleanup_tasks: list[Any] = field(default_factory=list)
