import logging
import os

from django.conf import settings
from openportal import is_config_loaded, load_config

logger = logging.getLogger(__name__)


def is_config_available():
    """Check if OpenPortal config is available without raising exceptions"""
    # First check if OpenPortal plugin is enabled
    if not settings.WALDUR_OPENPORTAL.get("ENABLED", False):
        return False

    return bool(os.environ.get("OPENPORTAL_CONFIG"))


def ensure_config_loaded():
    """Load config only if available and enabled, return success status

    Returns:
        bool: True if config loaded successfully or already loaded, False if disabled/unavailable
    """
    logger = logging.getLogger(__name__)

    # Check if OpenPortal plugin is enabled
    if not settings.WALDUR_OPENPORTAL.get("ENABLED", False):
        logger.debug("OpenPortal plugin is disabled, skipping config loading")
        return False

    if not is_config_loaded():
        config_file = os.environ.get("OPENPORTAL_CONFIG")
        if not config_file:
            logger.warning(
                "OPENPORTAL_CONFIG environment variable not set, skipping OpenPortal operations"
            )
            return False

        try:
            # this isn't thread-safe - we should make it thread-safe
            # in the OpenPortal python layer because load_config() likely
            # modifies global state without proper synchronization
            load_config(config_file)
            logger.debug(f"OpenPortal config loaded from {config_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to load OpenPortal config from '{config_file}': {e}")
            return False
    return True
