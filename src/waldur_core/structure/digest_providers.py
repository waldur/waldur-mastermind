import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DigestSectionData:
    """
    Structured data for one section of the project digest.
    Rendering to text/HTML happens later, per-user, under the correct locale.
    """

    key: str
    title_key: str
    order: int
    context: dict[str, Any]
    text_template: str
    html_template: str


class BaseDigestSectionProvider(ABC):
    """
    Base class for digest section providers.
    Each provider contributes one section to the project digest email.
    Apps register providers by placing them in {app}/project_digest.py.
    """

    section_key: str = ""
    section_title: str = ""
    section_title_key: str = ""
    # Display order of this section in the digest email (lower values appear first).
    order: int = 50

    @abstractmethod
    def get_section_data(self, project, period_start, period_end):
        """
        Collect structured data for a single project.
        Return None if this section has nothing to report.
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Whether this provider is available (e.g. feature flag, app installed).
        """
        pass


_providers: dict[str, BaseDigestSectionProvider] = {}


def register_provider(provider_class):
    instance = provider_class()
    _providers[instance.section_key] = instance
    logger.debug("Registered digest provider: %s", instance.section_key)


def get_provider(section_key) -> BaseDigestSectionProvider | None:
    return _providers.get(section_key)


def get_all_providers() -> dict[str, BaseDigestSectionProvider]:
    return dict(_providers)


def get_available_providers() -> list[BaseDigestSectionProvider]:
    """Returns providers that are currently available, sorted by order."""
    return sorted(
        [p for p in _providers.values() if p.is_available()],
        key=lambda p: p.order,
    )
