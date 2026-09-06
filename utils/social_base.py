# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Shared resolver for the configured N.E.K.O community origin."""

from __future__ import annotations

import os


DEFAULT_SOCIAL_BASE_URL = "https://community.project-neko.cn"


def configured_social_base_url() -> str | None:
    """Return the explicitly configured community origin, if present."""

    raw = (os.environ.get("NEKO_SOCIAL_BASE_URL", "") or "").strip().rstrip("/")
    return raw or None


def social_base_url() -> str:
    """Return the configured community origin or the production fallback."""

    return configured_social_base_url() or DEFAULT_SOCIAL_BASE_URL
