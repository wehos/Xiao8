from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from plugin._types.models import PluginMeta
from plugin.config.schema import (
    ConfigValidationError,
    validate_plugin_config,
    validate_plugin_config_partial,
)

pytestmark = pytest.mark.plugin_unit


def _base_config() -> dict[str, object]:
    return {
        "plugin": {
            "id": "schema_demo",
            "name": "Schema Demo",
            "entry": "plugins.schema_demo:SchemaDemoPlugin",
            "type": "plugin",
        },
    }


def test_plugin_runtime_startup_failure_accepts_known_policy() -> None:
    config = _base_config()
    config["plugin_runtime"] = {
        "timeout": 1.5,
        "startup_failure": "warn",
    }

    validated = validate_plugin_config(config)

    assert validated.plugin_runtime is not None
    assert validated.plugin_runtime.timeout == 1.5
    assert validated.plugin_runtime.startup_failure == "warn"


@pytest.mark.parametrize("timeout", [True, 0, -1, 300.1, "bad", float("nan"), float("inf"), float("-inf")])
def test_plugin_runtime_timeout_rejects_invalid_values(timeout: object) -> None:
    config = _base_config()
    config["plugin_runtime"] = {"timeout": timeout}

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert "timeout" in str(exc_info.value)


def test_plugin_runtime_startup_failure_rejects_unknown_policy() -> None:
    config = _base_config()
    config["plugin_runtime"] = {"startup_failure": "strict"}

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert "startup_failure" in str(exc_info.value)


@pytest.mark.parametrize("plugin_type", ["plugin", "adapter"])
def test_plugin_models_accept_active_plugin_types(plugin_type: str) -> None:
    meta = PluginMeta(id=f"{plugin_type}_demo", name="Demo", type=plugin_type)
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["type"] = plugin_type

    validated = validate_plugin_config(config)

    assert meta.type == plugin_type
    assert validated.plugin.type == plugin_type


@pytest.mark.parametrize("removed_type", ["script", "extension"])
def test_plugin_meta_rejects_removed_plugin_types(removed_type: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PluginMeta(id=f"legacy_{removed_type}", name="Legacy", type=removed_type)

    message = str(exc_info.value)
    assert "type" in message
    assert "必须" in message and "must be one of" in message and "必要があります" in message


@pytest.mark.parametrize("removed_type", ["script", "extension"])
def test_plugin_config_schema_rejects_removed_plugin_types(removed_type: str) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["type"] = removed_type

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert exc_info.value.field == "plugin.type"
    assert "必须" in exc_info.value.message
    assert "must be one of" in exc_info.value.message
    assert "必要があります" in exc_info.value.message


@pytest.mark.parametrize("removed_type", ["script", "extension"])
def test_partial_plugin_config_rejects_removed_plugin_types(removed_type: str) -> None:
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config_partial({"plugin": {"type": removed_type}})

    assert exc_info.value.field == "plugin.type"


def test_partial_plugin_config_rejects_removed_host_table() -> None:
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config_partial({"plugin": {"host": {"plugin_id": "owner"}}})

    assert exc_info.value.field == "plugin.host"
    assert "已随" in exc_info.value.message
    assert "was removed" in exc_info.value.message
    assert "削除されました" in exc_info.value.message


@pytest.mark.parametrize("plugin_type", ["plugin", "adapter"])
def test_partial_plugin_config_accepts_active_types(plugin_type: str) -> None:
    config = {"plugin": {"type": plugin_type}}

    assert validate_plugin_config_partial(config) is config


def _install_declaration() -> dict[str, object]:
    return {
        "enabled": True,
        "ui_i18n_dir": "i18n/ui",
        "tutorial_enabled": True,
        "kinds": {
            "rapidocr_models": {
                "entry_id": "demo_download_rapidocr_models",
                "label": "RapidOCR Models",
                "queued_message": "RapidOCR model download queued",
                "entry_timeout": 600.0,
            }
        },
    }


def test_plugin_install_declaration_is_optional_and_structured() -> None:
    without_install = validate_plugin_config(_base_config())
    assert without_install.plugin.install is None

    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["install"] = _install_declaration()

    validated = validate_plugin_config(config)

    assert validated.plugin.install is not None
    assert validated.plugin.install.enabled is True
    assert validated.plugin.install.ui_i18n_dir == "i18n/ui"
    assert set(validated.plugin.install.kinds) == {"rapidocr_models"}
    assert validated.plugin.install.kinds["rapidocr_models"].entry_timeout == 600.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_id", ""),
        ("entry_id", " padded"),
        ("label", ""),
        ("label", "padded "),
        ("queued_message", ""),
        ("queued_message", " padded "),
    ],
)
def test_plugin_install_rejects_empty_or_padded_kind_text(
    field: str,
    value: object,
) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    declaration = _install_declaration()
    kinds = declaration["kinds"]
    assert isinstance(kinds, dict)
    kind = kinds["rapidocr_models"]
    assert isinstance(kind, dict)
    kind[field] = value
    plugin["install"] = declaration

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "timeout",
    [True, 0, -1, "600", float("nan"), float("inf"), float("-inf")],
)
def test_plugin_install_rejects_invalid_entry_timeout(timeout: object) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    declaration = _install_declaration()
    kinds = declaration["kinds"]
    assert isinstance(kinds, dict)
    kind = kinds["rapidocr_models"]
    assert isinstance(kind, dict)
    kind["entry_timeout"] = timeout
    plugin["install"] = declaration

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert "entry_timeout" in str(exc_info.value)


@pytest.mark.parametrize("kind", ["RapidOCR", "rapid-ocr", " rapidocr", "rapidocr ", "1ocr"])
def test_plugin_install_rejects_noncanonical_kind(kind: str) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    declaration = _install_declaration()
    kinds = declaration["kinds"]
    assert isinstance(kinds, dict)
    kinds[kind] = kinds.pop("rapidocr_models")
    plugin["install"] = declaration

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert "kinds" in str(exc_info.value)


@pytest.mark.parametrize(
    "install",
    [
        {"enabled": False, "kinds": {"rapidocr_models": _install_declaration()["kinds"]["rapidocr_models"]}},
        {"enabled": False, "tutorial_enabled": True},
        {"enabled": False, "ui_i18n_dir": "i18n"},
    ],
)
def test_disabled_plugin_install_rejects_capabilities(install: dict[str, object]) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["install"] = install

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert "install" in str(exc_info.value)


@pytest.mark.parametrize(
    "install",
    [
        {"enabled": True, "unknown": True},
        {
            "enabled": True,
            "kinds": {
                "rapidocr_models": {
                    **_install_declaration()["kinds"]["rapidocr_models"],
                    "unknown": True,
                }
            },
        },
    ],
)
def test_plugin_install_rejects_unknown_fields(install: dict[str, object]) -> None:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["install"] = install

    with pytest.raises(ConfigValidationError):
        validate_plugin_config(config)


def test_model_requirements_are_optional_for_existing_plugins() -> None:
    validated = validate_plugin_config(_base_config())

    assert validated.plugin.models == {}


def _config_with_models(models: object) -> dict[str, object]:
    config = _base_config()
    plugin = config["plugin"]
    assert isinstance(plugin, dict)
    plugin["models"] = models
    return config


def test_model_requirements_parse_toml_and_preserve_usage_ids() -> None:
    config = tomllib.loads('''
[plugin]
id = "model_demo"
name = "Model Demo"
entry = "plugins.model_demo:ModelDemoPlugin"

[plugin.models.analysis]
label = "Content analysis"

[plugin.models.image_review]
label = "Image review"
description = "Optional image understanding"
required = false
capabilities = ["text", "image_input", "tool_calling", "streaming"]
''')

    validated = validate_plugin_config(config)

    assert set(validated.plugin.models) == {"analysis", "image_review"}
    analysis = validated.plugin.models["analysis"]
    assert analysis.label == "Content analysis"
    assert analysis.required is True
    assert analysis.capabilities == ["text"]
    review = validated.plugin.models["image_review"]
    assert review.required is False
    assert review.description == "Optional image understanding"
    assert review.capabilities == ["text", "image_input", "tool_calling", "streaming"]
    assert validated.model_dump()["plugin"]["models"]["image_review"] == {
        "label": "Image review",
        "description": "Optional image understanding",
        "required": False,
        "capabilities": ["text", "image_input", "tool_calling", "streaming"],
    }


@pytest.mark.parametrize("usage_id", ["", "Analysis", " image", "image ", "image-review", "../image", "1image", "a" * 65])
def test_model_requirements_reject_noncanonical_usage_ids(usage_id: str) -> None:
    config = _config_with_models({usage_id: {"label": "Analysis"}})

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert (exc_info.value.field or "").startswith("plugin.models.")


@pytest.mark.parametrize(
    "requirement",
    [
        {},
        {"label": ""},
        {"label": " padded "},
        {"label": 123},
        {"label": "Analysis", "required": "true"},
        {"label": "Analysis", "required": 1},
        {"label": "Analysis", "capabilities": ["audio_input"]},
        {"label": "Analysis", "capabilities": ["text", "text"]},
        {"label": "Analysis", "capabilities": "text"},
        {"label": "Analysis", "capabilites": ["text"]},
        {"label": "Analysis", "slot_id": "user_owned_slot"},
    ],
)
def test_model_requirements_reject_invalid_contracts(requirement: dict[str, object]) -> None:
    config = _config_with_models({"analysis": requirement})

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert (exc_info.value.field or "").startswith("plugin.models.analysis.")


@pytest.mark.parametrize("models", [None, [], "analysis"])
def test_model_requirements_must_be_a_table(models: object) -> None:
    config = _config_with_models(models)

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config(config)

    assert exc_info.value.field == "plugin.models"


def test_partial_plugin_config_preserves_valid_model_declarations() -> None:
    config = {"plugin": {"models": {"analysis": {"label": "Analysis"}}}}

    assert validate_plugin_config_partial(config) is config
    assert config["plugin"]["models"]["analysis"] == {"label": "Analysis"}


@pytest.mark.parametrize(
    ("models", "field"),
    [
        (None, "plugin.models"),
        ({"Bad-ID": {"label": "Analysis"}}, "plugin.models.Bad-ID.[key]"),
        ({"analysis": {"label": "Analysis", "required": "false"}}, "plugin.models.analysis.required"),
        ({"analysis": {"label": "Analysis", "capabilities": ["unknown"]}}, "plugin.models.analysis.capabilities.0"),
    ],
)
def test_partial_plugin_config_checks_model_declarations(models: object, field: str) -> None:
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_plugin_config_partial({"plugin": {"models": models}})

    assert exc_info.value.field == field
