# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Protocol constants shared by conversation-settings persistence paths."""

ALLOWED_CONVERSATION_SETTINGS = frozenset({
    "proactiveChatEnabled",
    "proactiveVisionEnabled",
    "proactiveVisionChatEnabled",
    "proactiveNewsChatEnabled",
    "proactiveCommunityChatEnabled",
    "proactiveVideoChatEnabled",
    "proactivePersonalChatEnabled",
    "proactiveMusicEnabled",
    "proactiveMemeEnabled",
    "proactiveMiniGameInviteEnabled",
    "mergeMessagesEnabled",
    "focusModeEnabled",
    "focusCognitionEnabled",
    "avatarReactionBubbleEnabled",
    "slopFilterEnabled",
    "proactiveChatInterval",
    "proactiveVisionInterval",
    "subtitleEnabled",
    "userLanguage",
    "textGuardMaxLength",
    "noiseReductionEnabled",
    "independentAsrEnabled",
    "voiceInputResourceOptimizationEnabled",
})
MAX_SAFE_ASR_WRITE_ID = 9_007_199_254_740_991
MAX_SAFE_CONVERSATION_SETTINGS_REVISION = 9_007_199_254_740_991
ASR_WRITE_ID_MAX_FUTURE_SKEW_MS = 365 * 24 * 60 * 60 * 1000
CONVERSATION_SETTINGS_RESET_KEY = "_conversation_settings_reset"
