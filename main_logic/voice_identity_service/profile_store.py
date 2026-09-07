"""Encrypted, atomic persistence for the single local speaker profile."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from pathlib import Path
import tempfile
import threading
from dataclasses import dataclass
from typing import Final, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import numpy as np

from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference

from .audio_contract import VoiceIdentityAudioContractSnapshot

try:
    import win32crypt
except ImportError:  # pragma: no cover - exercised through the platform guard
    win32crypt = None  # type: ignore[assignment]


_IS_WINDOWS: Final = os.name == "nt"
_SCHEMA_VERSION: Final = 3
_ALGORITHM: Final = "AES-256-GCM"
_KEY_WRAPPING: Final = "DPAPI-CURRENT-USER"
_AAD: Final = b"N.E.K.O.voice-identity.profile\x00v3"
_NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32
_DPAPI_UI_FORBIDDEN: Final = 0x1
_replace = os.replace


class VoiceIdentityProfileStoreError(RuntimeError):
    """Base class for safe profile persistence failures."""


class SecureStorageUnavailableError(VoiceIdentityProfileStoreError):
    """Raised when current-user secure key wrapping cannot be used."""


class VoiceIdentityProfileCorruptError(VoiceIdentityProfileStoreError):
    """Raised when an existing encrypted profile cannot be trusted."""


class VoiceIdentityProfileIncompatibleError(VoiceIdentityProfileCorruptError):
    """Raised when a trusted envelope uses an unsupported profile schema."""


@dataclass(frozen=True, slots=True)
class VoiceIdentityStoredProfile:
    """A decrypted provider profile paired with its processing-domain contract."""

    profile: SpeakerProfile
    audio_contract: VoiceIdentityAudioContractSnapshot

    def __post_init__(self) -> None:
        if type(self.profile) is not SpeakerProfile:
            raise TypeError("profile must be SpeakerProfile")
        if type(self.audio_contract) is not VoiceIdentityAudioContractSnapshot:
            raise TypeError("audio_contract must be VoiceIdentityAudioContractSnapshot")

    def close(self) -> None:
        self.profile.close()


class _KeyProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class WindowsDpapiKeyProtector:
    """Wrap AES keys with Windows DPAPI in the current-user scope."""

    def __init__(self) -> None:
        if not _IS_WINDOWS or win32crypt is None:
            raise SecureStorageUnavailableError("secure_storage_unavailable")

    def protect(self, plaintext: bytes) -> bytes:
        try:
            protected = win32crypt.CryptProtectData(
                plaintext,
                "N.E.K.O voice identity profile key",
                None,
                None,
                None,
                _DPAPI_UI_FORBIDDEN,
            )
        except Exception as exc:
            raise SecureStorageUnavailableError("secure_storage_unavailable") from exc
        if type(protected) is not bytes or not protected:
            raise SecureStorageUnavailableError("secure_storage_unavailable")
        return protected

    def unprotect(self, protected: bytes) -> bytes:
        try:
            _description, plaintext = win32crypt.CryptUnprotectData(
                protected,
                None,
                None,
                None,
                _DPAPI_UI_FORBIDDEN,
            )
        except Exception as exc:
            raise VoiceIdentityProfileCorruptError(
                "voice identity profile key could not be unwrapped"
            ) from exc
        if type(plaintext) is not bytes or len(plaintext) != _KEY_BYTES:
            raise VoiceIdentityProfileCorruptError(
                "voice identity profile key could not be unwrapped"
            )
        return plaintext


class VoiceIdentityProfileWrite:
    """A staged encrypted profile replacement owned by one store."""

    def __init__(
        self,
        store: VoiceIdentityProfileStore,
        temporary_path: Path,
    ) -> None:
        self._store = store
        self._temporary_path: Path | None = temporary_path
        self._state = "staged"
        self._lock = threading.Lock()

    @property
    def staged(self) -> bool:
        with self._lock:
            return self._state == "staged"

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._state == "committed"

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._state == "aborted"

    def commit(self) -> None:
        """Atomically publish the staged ciphertext; repeated calls are safe."""

        with self._lock:
            if self._state == "committed":
                return
            if self._state == "aborted":
                raise VoiceIdentityProfileStoreError(
                    "staged voice identity profile was aborted"
                )
            temporary_path = self._require_temporary_path()
            self._store._commit_staged(temporary_path)
            self._temporary_path = None
            self._state = "committed"

    async def acommit(self) -> None:
        """Run :meth:`commit` away from the event-loop thread."""

        await asyncio.to_thread(self.commit)

    def abort(self) -> None:
        """Remove unpublished ciphertext; repeated calls are safe."""

        with self._lock:
            if self._state != "staged":
                return
            temporary_path = self._require_temporary_path()
            self._store._abort_staged(temporary_path)
            self._temporary_path = None
            self._state = "aborted"

    async def aabort(self) -> None:
        """Run :meth:`abort` away from the event-loop thread."""

        await asyncio.to_thread(self.abort)

    def _require_temporary_path(self) -> Path:
        if self._temporary_path is None:
            raise VoiceIdentityProfileStoreError(
                "staged voice identity profile is unavailable"
            )
        return self._temporary_path


class VoiceIdentityProfileStore:
    """Persist exactly one encrypted profile with sync/async API parity."""

    def __init__(
        self,
        path: Path,
        *,
        key_protector: _KeyProtector | None = None,
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be pathlib.Path")
        self._path = path
        self._key_protector = key_protector or WindowsDpapiKeyProtector()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def save(
        self,
        profile: SpeakerProfile,
        *,
        audio_contract: VoiceIdentityAudioContractSnapshot,
    ) -> None:
        """Encrypt and atomically replace the stored profile."""

        staged = self.stage(profile, audio_contract=audio_contract)
        try:
            staged.commit()
        except BaseException:
            try:
                staged.abort()
            except BaseException:
                pass
            raise

    async def asave(
        self,
        profile: SpeakerProfile,
        *,
        audio_contract: VoiceIdentityAudioContractSnapshot,
    ) -> None:
        """Run :meth:`save` away from the event-loop thread."""

        await asyncio.to_thread(self.save, profile, audio_contract=audio_contract)

    def stage(
        self,
        profile: SpeakerProfile,
        *,
        audio_contract: VoiceIdentityAudioContractSnapshot,
    ) -> VoiceIdentityProfileWrite:
        """Write encrypted ciphertext without replacing the active profile."""

        if type(profile) is not SpeakerProfile:
            raise TypeError("profile must be SpeakerProfile")
        if type(audio_contract) is not VoiceIdentityAudioContractSnapshot:
            raise TypeError("audio_contract must be VoiceIdentityAudioContractSnapshot")
        with self._lock:
            encoded = self._encode(profile, audio_contract)
            temporary_path = self._write_temporary(encoded)
        return VoiceIdentityProfileWrite(self, temporary_path)

    async def astage(
        self,
        profile: SpeakerProfile,
        *,
        audio_contract: VoiceIdentityAudioContractSnapshot,
    ) -> VoiceIdentityProfileWrite:
        """Run :meth:`stage` away from the event-loop thread."""

        return await asyncio.to_thread(
            self.stage,
            profile,
            audio_contract=audio_contract,
        )

    def load(self) -> VoiceIdentityStoredProfile | None:
        """Decrypt the stored profile, returning ``None`` when absent."""

        with self._lock:
            try:
                encoded = self._path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise VoiceIdentityProfileStoreError(
                    "voice identity profile could not be read"
                ) from exc
            return self._decode(encoded)

    async def aload(self) -> VoiceIdentityStoredProfile | None:
        """Run :meth:`load` away from the event-loop thread."""

        return await asyncio.to_thread(self.load)

    def delete(self) -> bool:
        """Delete the encrypted profile if present."""

        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise VoiceIdentityProfileStoreError(
                    "voice identity profile could not be deleted"
                ) from exc
            return True

    async def adelete(self) -> bool:
        """Run :meth:`delete` away from the event-loop thread."""

        return await asyncio.to_thread(self.delete)

    def _encode(
        self,
        profile: SpeakerProfile,
        audio_contract: VoiceIdentityAudioContractSnapshot,
    ) -> bytes:
        reference = profile.clone_reference()
        embedding: np.ndarray | None = None
        data_key: bytearray | None = None
        try:
            data_key = bytearray(os.urandom(_KEY_BYTES))
            identity = reference.model_identity
            embedding = reference.copy_embedding()
            payload = {
                "audio_contract_id": audio_contract.contract_id,
                "audio_contract_revision": audio_contract.revision,
                "embedding": base64.b64encode(
                    embedding.astype("<f4", copy=False).tobytes(order="C")
                ).decode("ascii"),
                "embedding_dimension": identity.embedding_dimension,
                "generation": profile.generation,
                "model_id": identity.model_id,
                "model_revision": identity.model_revision,
                "noise_reduction_enabled": audio_contract.noise_reduction_enabled,
            }
            plaintext = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            wrapped_key = self._key_protector.protect(bytes(data_key))
            nonce = os.urandom(_NONCE_BYTES)
            ciphertext = AESGCM(bytes(data_key)).encrypt(nonce, plaintext, _AAD)
            envelope = {
                "algorithm": _ALGORITHM,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "key_wrapping": _KEY_WRAPPING,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "schema_version": _SCHEMA_VERSION,
                "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
            }
            return json.dumps(
                envelope,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except SecureStorageUnavailableError:
            raise
        except Exception as exc:
            raise VoiceIdentityProfileStoreError(
                "voice identity profile could not be encrypted"
            ) from exc
        finally:
            if data_key is not None:
                data_key[:] = b"\x00" * len(data_key)
            if embedding is not None:
                embedding.fill(0.0)
            reference.close()

    def _decode(self, encoded: bytes) -> VoiceIdentityStoredProfile:
        plaintext: bytearray | None = None
        data_key: bytearray | None = None
        embedding_bytes: bytearray | None = None
        embedding: np.ndarray | None = None
        reference: SpeakerReference | None = None
        profile: SpeakerProfile | None = None
        try:
            envelope = json.loads(encoded.decode("ascii"))
            if type(envelope) is not dict or set(envelope) != {
                "algorithm",
                "ciphertext",
                "key_wrapping",
                "nonce",
                "schema_version",
                "wrapped_key",
            }:
                raise ValueError("invalid envelope")
            if envelope["schema_version"] != _SCHEMA_VERSION:
                raise VoiceIdentityProfileIncompatibleError(
                    "voice identity profile schema is incompatible"
                )
            if envelope["algorithm"] != _ALGORITHM:
                raise ValueError("unsupported algorithm")
            if envelope["key_wrapping"] != _KEY_WRAPPING:
                raise ValueError("unsupported key wrapping")
            wrapped_key = _decode_base64(envelope["wrapped_key"])
            nonce = _decode_base64(envelope["nonce"])
            ciphertext = _decode_base64(envelope["ciphertext"])
            if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16:
                raise ValueError("invalid encrypted payload")
            data_key = bytearray(self._key_protector.unprotect(wrapped_key))
            if len(data_key) != _KEY_BYTES:
                raise ValueError("invalid data key")
            plaintext = bytearray(
                AESGCM(bytes(data_key)).decrypt(nonce, ciphertext, _AAD)
            )
            payload = json.loads(plaintext.decode("utf-8"))
            if type(payload) is not dict or set(payload) != {
                "audio_contract_id",
                "audio_contract_revision",
                "embedding",
                "embedding_dimension",
                "generation",
                "model_id",
                "model_revision",
                "noise_reduction_enabled",
            }:
                raise ValueError("invalid payload")
            dimension = payload["embedding_dimension"]
            if type(dimension) is not int or dimension <= 0:
                raise ValueError("invalid embedding dimension")
            embedding_bytes = bytearray(_decode_base64(payload["embedding"]))
            if len(embedding_bytes) != dimension * np.dtype("<f4").itemsize:
                raise ValueError("invalid embedding length")
            embedding = np.frombuffer(embedding_bytes, dtype="<f4").copy()
            identity = SpeakerModelIdentity(
                payload["model_id"],
                payload["model_revision"],
                dimension,
            )
            reference = SpeakerReference(identity, embedding)
            profile = SpeakerProfile(payload["generation"], reference)
            try:
                audio_contract = VoiceIdentityAudioContractSnapshot(
                    contract_id=payload["audio_contract_id"],
                    revision=payload["audio_contract_revision"],
                    noise_reduction_enabled=payload["noise_reduction_enabled"],
                )
            except (TypeError, ValueError) as exc:
                raise VoiceIdentityProfileIncompatibleError(
                    "voice identity audio contract is incompatible"
                ) from exc
            reference.close()
            reference = None
            stored_profile = VoiceIdentityStoredProfile(profile, audio_contract)
            profile = None
            return stored_profile
        except (SecureStorageUnavailableError, VoiceIdentityProfileCorruptError):
            raise
        except (
            InvalidTag,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            raise VoiceIdentityProfileCorruptError(
                "voice identity profile could not be decrypted or validated"
            ) from exc
        except Exception as exc:
            raise VoiceIdentityProfileStoreError(
                "voice identity profile could not be loaded"
            ) from exc
        finally:
            if profile is not None:
                profile.close()
            if reference is not None:
                reference.close()
            if embedding is not None:
                embedding.fill(0.0)
            if embedding_bytes is not None:
                embedding_bytes[:] = b"\x00" * len(embedding_bytes)
            if data_key is not None:
                data_key[:] = b"\x00" * len(data_key)
            if plaintext is not None:
                plaintext[:] = b"\x00" * len(plaintext)

    def _write_temporary(self, encoded: bytes) -> Path:
        parent = self._path.parent
        temporary_path: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            completed_path = temporary_path
            temporary_path = None
            return completed_path
        except OSError as exc:
            raise VoiceIdentityProfileStoreError(
                "voice identity profile could not be staged"
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _commit_staged(self, temporary_path: Path) -> None:
        with self._lock:
            try:
                _replace(temporary_path, self._path)
            except OSError as exc:
                raise VoiceIdentityProfileStoreError(
                    "voice identity profile could not be committed"
                ) from exc

    def _abort_staged(self, temporary_path: Path) -> None:
        with self._lock:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise VoiceIdentityProfileStoreError(
                    "staged voice identity profile could not be removed"
                ) from exc


def _decode_base64(value: object) -> bytes:
    if type(value) is not str:
        raise TypeError("encrypted field must be a string")
    return base64.b64decode(value, validate=True)
