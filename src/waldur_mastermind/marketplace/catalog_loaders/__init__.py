# Catalog loader framework for unified software catalog system
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


def detect_eessi_version(api_base_url: str) -> str:
    """Detect latest EESSI version from API metadata.

    Lightweight HTTP call that returns only the version string,
    without instantiating a full loader or downloading package data.
    """
    api_base_url = api_base_url.rstrip("/")
    response = requests.get(
        f"{api_base_url}/eessi_api_metadata_software.json", timeout=30
    )
    response.raise_for_status()
    data = response.json()

    versions = list(data.get("architectures_map", {}).keys())
    return max(versions) if versions else "unknown"


def detect_spack_version(data_url: str) -> str:
    """Detect latest Spack version from data timestamp.

    Lightweight HTTP call that returns only the version string,
    without instantiating a full loader or downloading all package data.
    """
    response = requests.get(data_url, timeout=30)
    response.raise_for_status()
    data = response.json()

    last_update = data.get("last_update", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    date_part = last_update.split()[0]
    return date_part.replace("-", ".")
