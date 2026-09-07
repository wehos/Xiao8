"""Safe correlation and scalar projection for speaker diagnostic events."""

import hashlib
import secrets
import time
from dataclasses import fields
from weakref import WeakKeyDictionary

from config.application import APP_VERSION

from .speaker_shadow.diagnostics import SpeakerShadowDiagnostic

_PROCESS_NONCE = secrets.token_bytes(16)
_RUNTIME_REFS: WeakKeyDictionary = WeakKeyDictionary()


def diagnostic_context(runtime: object, epoch: int) -> dict:
    # Object addresses can be reused after teardown; weak random identities
    # prevent a new runtime at the same address borrowing an old session ref.
    runtime_ref = _RUNTIME_REFS.get(runtime)
    if runtime_ref is None:
        runtime_ref = secrets.token_hex(12)
        _RUNTIME_REFS[runtime] = runtime_ref
    return {
        "schema": 2,
        "app_version": APP_VERSION,
        "observed_at_ns": time.time_ns(),
        "diagnostic_session_ref": hashlib.blake2s(
            f"{runtime_ref}:{epoch}".encode(), key=_PROCESS_NONCE, digest_size=12,
        ).hexdigest(),
        "session_epoch": epoch,
    }


def speaker_diagnostic_scalars(event: SpeakerShadowDiagnostic) -> dict:
    # The immutable event contains only explicit scalars and a candidate key.
    result = {item.name: getattr(event, item.name) for item in fields(event)
              if item.name != "candidate"}
    result.update(
        detector_epoch=event.candidate.detector_epoch,
        shadow_generation=event.candidate.shadow_generation,
        candidate_scope=event.candidate.scope,
    )
    return result
