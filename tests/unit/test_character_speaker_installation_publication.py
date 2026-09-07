import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(50))
async def test_new_manager_is_not_published_until_desired_spec_bound(monkeypatch, iteration):
    from app.main_server import character_runtime, voice_identity_runtime

    name = "speaker-publication-regression"
    entered, resume = asyncio.Event(), asyncio.Event()
    created = []

    class Manager:
        websocket = None
        is_active = False
        is_starting = False

        def __init__(self, *_args):
            self.bound = False
            self.aclose = AsyncMock()
            created.append(self)

    role = SimpleNamespace(
        websocket_lock=asyncio.Lock(), session_manager=None,
        sync_task=SimpleNamespace(done=lambda: False), sync_message_queue=object(),
    )

    async def register(manager):
        entered.set()
        await resume.wait()
        manager.bound = True
        return True

    monkeypatch.setitem(character_runtime.role_state, name, role)
    monkeypatch.setattr(character_runtime, "lanlan_prompt", {name: "prompt"})
    monkeypatch.setattr(character_runtime, "master_name", "Master")
    monkeypatch.setattr(character_runtime.core, "LLMSessionManager", Manager)
    monkeypatch.setattr(voice_identity_runtime, "register_voice_identity_manager", register)
    operation = asyncio.create_task(character_runtime._init_character_resources(name, False))
    try:
        await entered.wait()
        assert len(created) == 1
        assert role.session_manager is None
        resume.set()
        await operation
        assert role.session_manager is created[0]
        assert role.session_manager.bound
    finally:
        resume.set()
        await asyncio.gather(operation, return_exceptions=True)
