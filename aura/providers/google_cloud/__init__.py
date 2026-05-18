"""Google Cloud / Vertex AI provider for Aura.

All google-genai SDK imports are deferred so the module can be imported
without the optional dependency installed.

Exports:
    GoogleCloudClient       — streaming client for Vertex AI
    get_google_cloud_config — read env vars for Google Cloud
    classify_error          — map HTTP status codes to error classes
    CooldownManager         — thread-safe rate-limit cooldown
    encode_signature_safe   — bytes → base64, never str(bytes)
    make_message_json_safe  — recursively sanitize dicts of raw bytes
    detect_auth_mode        — 'adc' if ADC creds exist, else 'unknown'
    is_configured           — True if GOOGLE_CLOUD_PROJECT is set
    GOOGLE_CLOUD_PROJECT_ENV — env var name
"""

from aura.providers.google_cloud.client import GoogleCloudClient
from aura.providers.google_cloud.config import (
    GOOGLE_CLOUD_PROJECT_ENV,
    get_google_cloud_config,
    is_configured,
)
from aura.providers.google_cloud.cooldown import CooldownManager
from aura.providers.google_cloud.errors import classify_error
from aura.providers.google_cloud.signatures import encode_signature_safe, make_message_json_safe
from aura.providers.google_cloud.auth import detect_auth_mode
