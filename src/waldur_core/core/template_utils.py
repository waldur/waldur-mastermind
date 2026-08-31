from functools import cache

from django.template.engine import Engine
from django.template.loaders.app_directories import Loader


@cache
def get_original_content(path):
    """
    Return the built-in filesystem content for *path*, bypassing any DB override.

    Cached: filesystem templates don't change within a process's lifetime, and
    this is called once per row by the is_overridden filter, which evaluates
    before pagination.
    """
    loader = Loader(Engine())
    for origin in loader.get_template_sources(path):
        try:
            source = loader.get_contents(origin)
        except Exception:
            continue
        if source:
            return source
    return None


def is_template_overridden(template):
    """True if *template*'s stored content differs from its filesystem default."""
    return bool(template.content) and template.content != get_original_content(
        template.path
    )
