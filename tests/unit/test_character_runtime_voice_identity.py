"""Character lifecycle wiring for Owner voice-identity register/unregister.

Covers character_runtime calling into voice_identity_runtime on the three
session_manager paths (create, hot-swap, delete), plus the websocket_lock
transaction when deletion races slot initialization.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_remove_one_catgirl_unregisters_voice_identity_manager(monkeypatch):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoVoiceDelete"
    calls = []

    class Manager:
        async def aclose(self):
            calls.append(("close", self))

    manager = Manager()

    async def unregister(target):
        calls.append(("unregister", target))

    async def refresh_globals():
        return None

    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=manager,
        sync_task=None,
        sync_message_queue=SimpleNamespace(empty=lambda: True),
    )
    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )
    monkeypatch.setattr(
        character_runtime,
        "_refresh_character_globals",
        refresh_globals,
    )

    await character_runtime.remove_one_catgirl(name)

    assert calls == [("unregister", manager), ("close", manager)]
    assert role.websocket_lock.locked() is False
    assert name not in character_runtime.role_state


@pytest.mark.asyncio
async def test_initialize_character_data_unregisters_removed_voice_managers(
    monkeypatch,
):
    from app.main_server import character_runtime

    removed_name = "NekoVoiceRemoved"
    calls = []

    class Manager:
        async def aclose(self):
            calls.append(("close", self))

    manager = Manager()
    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=manager,
        sync_task=None,
        sync_message_queue=SimpleNamespace(empty=lambda: True),
    )
    monkeypatch.setitem(character_runtime.role_state, removed_name, role)
    from app.main_server import voice_identity_runtime

    async def unregister(target):
        calls.append(("unregister", target))

    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )
    monkeypatch.setattr(
        character_runtime._config_manager,
        "cleanup_invalid_voice_ids",
        lambda: (False, []),
    )
    monkeypatch.setattr(character_runtime, "catgirl_names", [])

    async def refresh_globals():
        return None

    async def stop_character_thread(name):
        calls.append(("stop", name))

    monkeypatch.setattr(
        character_runtime,
        "_refresh_character_globals",
        refresh_globals,
    )
    monkeypatch.setattr(
        character_runtime,
        "_stop_character_thread",
        stop_character_thread,
    )

    await character_runtime.initialize_character_data()

    assert calls == [
        ("stop", removed_name),
        ("unregister", manager),
        ("close", manager),
    ]
    assert removed_name not in character_runtime.role_state


@pytest.mark.asyncio
async def test_inactive_manager_replacement_closes_terminal_before_install(monkeypatch):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoVoiceReplace"
    calls = []

    class OldManager:
        websocket = None
        is_active = False
        is_starting = False
        user_language = "zh-CN"
        _user_language_explicit = True

        async def aclose(self):
            calls.append(("close", self))

    class NewManager:
        websocket = None
        is_active = False
        is_starting = False

        def __init__(self, queue, lanlan_name, prompt):
            calls.append(("construct", self))
            self.queue = queue
            self.lanlan_name = lanlan_name
            self.prompt = prompt
            self.websocket_lock = None

    old_manager = OldManager()
    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=old_manager,
        sync_task=SimpleNamespace(done=lambda: False),
        sync_message_queue=object(),
    )

    async def unregister(target):
        calls.append(("unregister", target))

    async def register(target):
        calls.append(("register", target))
        return True

    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(character_runtime, "lanlan_prompt", {name: "prompt"})
    monkeypatch.setattr(character_runtime, "master_name", "Master")
    monkeypatch.setattr(character_runtime.core, "LLMSessionManager", NewManager)
    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )
    monkeypatch.setattr(
        voice_identity_runtime,
        "register_voice_identity_manager",
        register,
    )

    await character_runtime._init_character_resources(name, False)

    new_manager = role.session_manager
    assert isinstance(new_manager, NewManager)
    assert calls == [
        ("unregister", old_manager),
        ("close", old_manager),
        ("construct", new_manager),
        ("register", new_manager),
    ]
    assert new_manager.user_language == "zh-CN"
    assert new_manager._user_language_explicit is True


@pytest.mark.asyncio
async def test_remove_preserves_slot_when_terminal_manager_close_fails(monkeypatch):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoVoiceDeleteCloseFailure"
    calls = []

    class Manager:
        async def aclose(self):
            calls.append(("close", self))
            raise RuntimeError("terminal close failed")

    manager = Manager()
    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=manager,
        sync_task=None,
        sync_message_queue=SimpleNamespace(empty=lambda: True),
    )

    async def unregister(target):
        calls.append(("unregister", target))

    async def refresh_globals():
        return None

    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )
    monkeypatch.setattr(
        character_runtime,
        "_refresh_character_globals",
        refresh_globals,
    )

    with pytest.raises(RuntimeError, match="terminal close failed"):
        await character_runtime.remove_one_catgirl(name)

    assert calls == [("unregister", manager), ("close", manager)]
    assert character_runtime.role_state[name] is role
    assert role.session_manager is manager
    assert role.websocket_lock.locked() is False


@pytest.mark.asyncio
async def test_inactive_replacement_preserves_owner_when_terminal_close_fails(
    monkeypatch,
):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoVoiceReplaceCloseFailure"
    calls = []

    class OldManager:
        websocket = None
        is_active = False
        is_starting = False

        async def aclose(self):
            calls.append(("close", self))
            raise RuntimeError("terminal close failed")

    class NewManager:
        def __init__(self, *_args, **_kwargs):
            calls.append(("construct", self))

    old_manager = OldManager()
    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=old_manager,
        sync_task=SimpleNamespace(done=lambda: False),
        sync_message_queue=object(),
    )

    async def unregister(target):
        calls.append(("unregister", target))

    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(character_runtime, "lanlan_prompt", {name: "prompt"})
    monkeypatch.setattr(character_runtime, "master_name", "Master")
    monkeypatch.setattr(character_runtime.core, "LLMSessionManager", NewManager)
    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )

    with pytest.raises(RuntimeError, match="terminal close failed"):
        await character_runtime._init_character_resources(name, False)

    assert calls == [("unregister", old_manager), ("close", old_manager)]
    assert role.session_manager is old_manager
    assert role.websocket_lock.locked() is False


@pytest.mark.asyncio
async def test_remove_waiting_for_init_lock_cancels_new_connector(monkeypatch):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoConcurrentConnectorDelete"
    register_started = asyncio.Event()
    allow_register = asyncio.Event()
    unregistered = []

    class NewManager:
        websocket = None
        is_active = False
        is_starting = False

        def __init__(self, queue, lanlan_name, prompt):
            self.websocket_lock = None

    async def register(target):
        register_started.set()
        await allow_register.wait()
        return True

    async def unregister(target):
        unregistered.append(target)

    async def run_connector(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def refresh_globals():
        return None

    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=None,
        sync_task=None,
        sync_message_queue=SimpleNamespace(empty=lambda: True),
    )
    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(character_runtime, "lanlan_prompt", {name: "prompt"})
    monkeypatch.setattr(character_runtime, "master_name", "Master")
    monkeypatch.setattr(character_runtime.core, "LLMSessionManager", NewManager)
    monkeypatch.setattr(
        character_runtime.cross_server,
        "run_sync_connector",
        run_connector,
    )
    monkeypatch.setattr(character_runtime, "_refresh_character_globals", refresh_globals)
    monkeypatch.setattr(voice_identity_runtime, "register_voice_identity_manager", register)
    monkeypatch.setattr(
        voice_identity_runtime,
        "unregister_voice_identity_manager",
        unregister,
    )

    init_task = asyncio.create_task(
        character_runtime._init_character_resources(name, False)
    )
    await asyncio.wait_for(register_started.wait(), 1.0)
    remove_task = asyncio.create_task(character_runtime.remove_one_catgirl(name))
    await asyncio.sleep(0)
    assert not remove_task.done()

    allow_register.set()
    await init_task
    connector_task = role.sync_task
    assert connector_task is not None
    await remove_task

    assert connector_task.cancelled()
    assert unregistered == [role.session_manager]
    assert name not in character_runtime.role_state


@pytest.mark.asyncio
async def test_character_init_skips_slot_removed_while_waiting_for_lock(
    monkeypatch,
):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "NekoConcurrentDelete"
    registered = []

    async def register(target):
        registered.append(target)
        return True

    class NewManager:
        websocket = None
        is_active = False
        is_starting = False

        def __init__(self, queue, lanlan_name, prompt):
            self.queue = queue
            self.lanlan_name = lanlan_name
            self.prompt = prompt
            self.websocket_lock = None

    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=None,
        sync_message_queue=object(),
        sync_task=SimpleNamespace(done=lambda: False),
    )
    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(character_runtime, "lanlan_prompt", {name: "prompt"})
    monkeypatch.setattr(character_runtime, "master_name", "Master")
    monkeypatch.setattr(character_runtime.core, "LLMSessionManager", NewManager)
    monkeypatch.setattr(
        voice_identity_runtime,
        "register_voice_identity_manager",
        register,
    )

    await role.websocket_lock.acquire()
    init_task = asyncio.create_task(
        character_runtime._init_character_resources(name, False)
    )
    await asyncio.sleep(0)
    del character_runtime.role_state[name]
    role.websocket_lock.release()
    await init_task

    assert registered == []
    assert name not in character_runtime.role_state


@pytest.mark.asyncio
async def test_character_init_skips_slot_removed_before_first_schedule(monkeypatch):
    from app.main_server import character_runtime

    name = "NekoDeletedBeforeInitSchedule"
    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(),
        session_manager=None,
        sync_message_queue=SimpleNamespace(empty=lambda: True),
        sync_task=None,
    )
    monkeypatch.setitem(character_runtime.role_state, name, role)

    init = character_runtime._init_character_resources(name, False)
    await character_runtime._unregister_and_cleanup_character_slot(name)
    results = await asyncio.gather(init, return_exceptions=True)

    assert results == [None]
    assert name not in character_runtime.role_state
