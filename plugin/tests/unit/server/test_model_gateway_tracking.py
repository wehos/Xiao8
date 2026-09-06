"""Inherited OpenAI hooks must not double-count the local plugin gateway hop."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai.resources.chat.completions import AsyncCompletions, Completions

from utils.token_tracker import hooks


pytestmark = pytest.mark.plugin_unit


_GATEWAY_URLS = (
    "http://127.0.0.1:48916/api/models/v1/",
    "http://127.0.0.1:48916/api/models/v1",
)
_PROVIDER_URLS = (
    "http://localhost:48916/api/models/v1",
    "http://[::1]:48916/api/models/v1",
    "http://127.0.0.1:9000/api/models/v1",
    "https://127.0.0.1:48916/api/models/v1",
    "http://127.0.0.1:48916/api/models/v1?other=1",
    "https://api.openai.com/v1/",
    "https://provider.test/api/models/v1/",
    "http://127.0.0.1:48916/v1/",
    "http://127.0.0.1:48916/api/models/v1-extra/",
    "https://localhost.provider.test/api/models/v1/",
    "https://127.0.0.1.provider.test/api/models/v1/",
    "ftp://localhost/api/models/v1/",
)


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    from config import network
    monkeypatch.delenv("NEKO_USER_PLUGIN_SERVER_PORT", raising=False)
    monkeypatch.setattr(network, "USER_PLUGIN_BASE", "http://127.0.0.1:48916")


def usage_response():
    return SimpleNamespace(
        model="test-model",
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )


class UsageStream:
    def __iter__(self):
        yield usage_response()

    async def __aiter__(self):
        yield usage_response()


@pytest.fixture
def installed_hooks(monkeypatch):
    calls = []
    result = SimpleNamespace(error=None, value=None)
    record = Mock()

    def original_create(self, *args, **kwargs):
        calls.append((args, kwargs))
        if result.error is not None:
            raise result.error
        result.value = UsageStream() if kwargs.get("stream") else usage_response()
        return result.value

    async def original_async_create(self, *args, **kwargs):
        return original_create(self, *args, **kwargs)

    monkeypatch.setattr(Completions, "create", original_create)
    monkeypatch.setattr(AsyncCompletions, "create", original_async_create)
    monkeypatch.setattr(hooks, "_install_crash_excepthook", lambda: None)
    monkeypatch.setattr(hooks, "_stream_options_blocklist", set())
    monkeypatch.setattr(hooks.TokenTracker, "get_instance", lambda: SimpleNamespace(record=record))
    hooks.install_hooks()
    return SimpleNamespace(calls=calls, result=result, record=record)


def completion_resource(provider_base_url):
    return SimpleNamespace(_client=SimpleNamespace(base_url=httpx.URL(provider_base_url)))


@pytest.mark.parametrize("provider_base_url", _GATEWAY_URLS)
@pytest.mark.parametrize("streaming", [False, True])
def test_sync_gateway_call_bypasses_tracking_and_stream_option_injection(installed_hooks, provider_base_url, streaming):
    state = installed_hooks
    response = Completions.create(completion_resource(provider_base_url), model="analysis", stream=streaming)
    assert response is state.result.value
    if streaming:
        assert len(list(response)) == 1
    assert state.calls == [((), {"model": "analysis", "stream": streaming})]
    state.record.assert_not_called()


@pytest.mark.parametrize("provider_base_url", _GATEWAY_URLS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_async_gateway_call_bypasses_tracking_and_stream_option_injection(installed_hooks, provider_base_url, streaming):
    state = installed_hooks
    response = await AsyncCompletions.create(completion_resource(provider_base_url), model="analysis", stream=streaming)
    assert response is state.result.value
    if streaming:
        assert len([part async for part in response]) == 1
    assert state.calls == [((), {"model": "analysis", "stream": streaming})]
    state.record.assert_not_called()


@pytest.mark.parametrize("streaming", [False, True])
def test_sync_gateway_failure_is_not_recorded_or_retried(installed_hooks, streaming):
    state = installed_hooks
    state.result.error = RuntimeError("gateway failure")
    with pytest.raises(RuntimeError) as error:
        Completions.create(completion_resource(_GATEWAY_URLS[0]), model="analysis", stream=streaming)
    assert error.value is state.result.error
    assert state.calls == [((), {"model": "analysis", "stream": streaming})]
    state.record.assert_not_called()


@pytest.mark.parametrize("streaming", [False, True])
async def test_async_gateway_failure_is_not_recorded_or_retried(installed_hooks, streaming):
    state = installed_hooks
    state.result.error = RuntimeError("gateway failure")
    with pytest.raises(RuntimeError) as error:
        await AsyncCompletions.create(completion_resource(_GATEWAY_URLS[0]), model="analysis", stream=streaming)
    assert error.value is state.result.error
    assert state.calls == [((), {"model": "analysis", "stream": streaming})]
    state.record.assert_not_called()


@pytest.mark.parametrize("provider_base_url", _PROVIDER_URLS)
@pytest.mark.parametrize("streaming", [False, True])
def test_sync_other_provider_calls_keep_existing_tracking(installed_hooks, provider_base_url, streaming):
    state = installed_hooks
    response = Completions.create(completion_resource(provider_base_url), model="test-model", stream=streaming)
    if streaming:
        list(response)
        assert state.calls[0][1]["stream_options"] == {"include_usage": True}
    state.record.assert_called_once_with(
        model="test-model", prompt_tokens=7, completion_tokens=3, total_tokens=10, cached_tokens=0, call_type="unknown",
    )


@pytest.mark.parametrize("provider_base_url", _PROVIDER_URLS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_async_other_provider_calls_keep_existing_tracking(installed_hooks, provider_base_url, streaming):
    state = installed_hooks
    response = await AsyncCompletions.create(completion_resource(provider_base_url), model="test-model", stream=streaming)
    if streaming:
        _ = [part async for part in response]
        assert state.calls[0][1]["stream_options"] == {"include_usage": True}
    state.record.assert_called_once_with(
        model="test-model", prompt_tokens=7, completion_tokens=3, total_tokens=10, cached_tokens=0, call_type="unknown",
    )


@pytest.mark.parametrize("streaming", [False, True])
def test_sync_provider_failures_keep_existing_tracking(installed_hooks, streaming):
    state = installed_hooks
    state.result.error = RuntimeError("upstream failure")
    with pytest.raises(RuntimeError):
        Completions.create(completion_resource(_PROVIDER_URLS[0]), model="test-model", stream=streaming)
    state.record.assert_called_once_with(
        model="test-model", prompt_tokens=0, completion_tokens=0, total_tokens=0, call_type="unknown", success=False,
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_async_provider_failures_keep_existing_tracking(installed_hooks, streaming):
    state = installed_hooks
    state.result.error = RuntimeError("upstream failure")
    with pytest.raises(RuntimeError):
        await AsyncCompletions.create(completion_resource(_PROVIDER_URLS[0]), model="test-model", stream=streaming)
    state.record.assert_called_once_with(
        model="test-model", prompt_tokens=0, completion_tokens=0, total_tokens=0, call_type="unknown", success=False,
    )


async def test_gateway_explicit_stream_options_are_preserved(installed_hooks):
    options = {"include_usage": False}
    await AsyncCompletions.create(
        completion_resource(_GATEWAY_URLS[0]), model="analysis", stream=True, stream_options=options,
    )
    assert installed_hooks.calls[0][1]["stream_options"] == options
    installed_hooks.record.assert_not_called()


def test_hook_installation_remains_idempotent(installed_hooks):
    original = Completions.create, AsyncCompletions.create
    hooks.install_hooks()
    hooks.install_hooks()
    assert original == (Completions.create, AsyncCompletions.create)
    Completions.create(completion_resource(_PROVIDER_URLS[0]), model="test-model")
    installed_hooks.record.assert_called_once()


@pytest.mark.parametrize("provider_base_url", ["", "http://[invalid/api/models/v1", "http://remote.test/api/models/v1"])
def test_invalid_or_remote_gateway_urls_do_not_disable_tracking(provider_base_url):
    assert not hooks._is_plugin_model_gateway(provider_base_url)


def test_bypass_follows_launcher_selected_port(monkeypatch):
    monkeypatch.setenv("NEKO_USER_PLUGIN_SERVER_PORT", "51234")
    assert hooks._is_plugin_model_gateway("http://127.0.0.1:51234/api/models/v1")
    assert not hooks._is_plugin_model_gateway("http://localhost:51234/api/models/v1/")
    assert not hooks._is_plugin_model_gateway("http://127.0.0.1:48916/api/models/v1")
    assert not hooks._is_plugin_model_gateway("https://localhost:51234/api/models/v1")


def test_bypass_matches_configured_scheme_and_effective_port(monkeypatch):
    from config import network
    monkeypatch.setattr(network, "USER_PLUGIN_BASE", "https://localhost")
    assert hooks._is_plugin_model_gateway("https://LOCALHOST:443/api/models/v1")
    assert not hooks._is_plugin_model_gateway("https://127.0.0.1:443/api/models/v1")
    assert not hooks._is_plugin_model_gateway("http://localhost:443/api/models/v1")
    assert not hooks._is_plugin_model_gateway("https://localhost:444/api/models/v1")
    assert not hooks._is_plugin_model_gateway("https://localhost:0/api/models/v1")
