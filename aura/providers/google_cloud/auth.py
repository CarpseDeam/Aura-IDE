import os
from pathlib import Path

import sys

ADC_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

if sys.platform == "win32":
    _DEFAULT_ADC_PATH = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
else:
    _DEFAULT_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def check_adc_file() -> str | None:
    """Return the path to ADC credentials if they exist, else None.

    Checks the GOOGLE_APPLICATION_CREDENTIALS env var first, then the
    default gcloud ADC path.
    """
    env_path = os.environ.get(ADC_ENV)
    if env_path and Path(env_path).exists():
        return env_path
    if _DEFAULT_ADC_PATH.exists():
        return str(_DEFAULT_ADC_PATH)
    return None


def detect_auth_mode() -> str:
    """Return 'adc' if ADC credentials can be found, otherwise 'unknown'.

    This is a purely local check — no network calls.
    """
    if check_adc_file() is not None:
        return "adc"
    return "unknown"
