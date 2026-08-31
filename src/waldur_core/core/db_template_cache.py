"""
Cache helpers for DatabaseTemplateLoader.

Mirrors the cache-then-DB-then-notfound flow django-dbtemplates used, scoped to
Waldur's single-tenant NotificationTemplate content (no Sites-framework support,
which nothing here ever relied on).
"""

from django.core.cache import cache

CACHE_KEY_PREFIX = "notification_template"


def get_cache_key(path):
    # The raw path, not a slugified version: slugify() strips "/" and "."
    # (get_cache_key("a/b.html") == get_cache_key("ab.html")), which used to be
    # harmless when a name collision merely meant two filesystem paths shared a
    # cache slot with identical content. It stopped being harmless once content
    # lives on the same row as the path, so a real name isn't worth the risk.
    return f"{CACHE_KEY_PREFIX}::{path}"


def get_cache_notfound_key(path):
    return get_cache_key(path) + "::notfound"


def add_template_to_cache(instance, **kwargs):
    """
    Refresh the cache entry for *instance* after its content was created or changed.

    Removes any stale positive cache entry and the "notfound" sentinel, then writes
    the new content, so the change is served on the very next template render
    without a process restart. Also wired as a post_save receiver on
    NotificationTemplate (see core/apps.py) so this happens on *every* save - a bare
    NotificationTemplate.objects.create()/save() must not leave a stale "notfound"
    sentinel in place from an earlier render, the way an explicit-call-only design
    would.

    Blank content (e.g. after override_templates --clean resets an override) is
    not a valid positive entry - caching it verbatim would make the loader treat
    an empty string as a cache hit and never fall through to the filesystem
    template. Plant the "notfound" sentinel instead, matching what the loader
    itself does on a blank-content miss.

    **kwargs absorbs the extra arguments Django's post_save signal passes
    (sender, created, raw, using, update_fields) when connected as a receiver.
    """
    if instance.content:
        cache.delete(get_cache_notfound_key(instance.path))
        cache.set(get_cache_key(instance.path), instance.content)
    else:
        cache.delete(get_cache_key(instance.path))
        cache.set(get_cache_notfound_key(instance.path), True)


def remove_cached_template(instance, **kwargs):
    """Also wired as a pre_delete receiver on NotificationTemplate (see core/apps.py)."""
    cache.delete(get_cache_key(instance.path))
