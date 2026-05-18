import os

GOOGLE_CLOUD_PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"
GOOGLE_CLOUD_LOCATION_ENV = "GOOGLE_CLOUD_LOCATION"
GOOGLE_GENAI_USE_VERTEXAI_ENV = "GOOGLE_GENAI_USE_VERTEXAI"
DEFAULT_LOCATION = "global"


def get_google_cloud_project() -> str | None:
    """Return the Google Cloud project name, or None if not set."""
    return os.environ.get(GOOGLE_CLOUD_PROJECT_ENV)


def get_google_cloud_location() -> str:
    """Return the Google Cloud location, defaulting to 'global'."""
    return os.environ.get(GOOGLE_CLOUD_LOCATION_ENV, DEFAULT_LOCATION)


def get_google_cloud_config() -> dict[str, str | None]:
    """Return a dict summarising the Google Cloud config from env vars."""
    return {
        "project": get_google_cloud_project(),
        "location": get_google_cloud_location(),
        "use_vertexai": os.environ.get(GOOGLE_GENAI_USE_VERTEXAI_ENV),
    }


def is_configured() -> bool:
    """Return True if a Google Cloud project is set."""
    return get_google_cloud_project() is not None
