"""
Generic Resource Resolver Registry for the Support Application.

== Purpose ==

This module provides a generic, key-based registry to decouple the core
(e.g. `waldur_support`) application from its various resource-providing plugins.
It allows plugins to register "resolver" functions that can find a resource
based on a specific attribute (e.g., an external IP, an internal IP, a MAC
address, etc.).

== How It Works ==

1.  **Registry:** A central dictionary (`_resolver_registry`) maps a "lookup key"
    (string) to a list of resolver functions. The lookup key typically matches
    the name of the filter field that will use it (e.g., 'resource_external_ip').

2.  **Registration:** Plugins register their resolver functions using the
    `@register_resolver(lookup_key)` decorator. This decorator takes the key
    for which the function should be registered.

3.  **The Contract:** Any registered resolver function MUST:
    - Accept one argument: the value to search for (e.g., an IP address string).
    - Return a list of two-element tuples: `(content_type_id, object_id)`.
    - Return an empty list `[]` if no resources are found.

4.  **Usage:** The core support application's `IssueFilter` calls the public
    function `get_resource_ids(lookup_key, value)`. This function finds all
    resolvers registered for the given `lookup_key`, executes them, and
    aggregates their results into a single list.

== Example: Registering a 'resource_name' resolver ==

// In a plugin's handlers.py
from waldur_support.registry import register_resolver
from . import models

@register_resolver("resource_name")
def find_instance_by_name(name: str) -> list[tuple[int, int]]:
    content_type = ContentType.objects.get_for_model(models.Instance)
    instances = models.Instance.objects.filter(name=name)
    return [(content_type.id, instance.pk) for instance in instances]

"""

import logging
from collections import defaultdict
from functools import reduce
from operator import or_

from django.db.models import Q

# The registry is now a dictionary mapping a lookup_key (str) to a list of functions.
_resolver_registry = defaultdict(list)


logger = logging.getLogger(__name__)


def register_resolver(lookup_key: str):
    """
    A decorator factory for registering a resolver function for a specific lookup key.

    Args:
        lookup_key: A string identifier, which should correspond to a filter
                    field name (e.g., 'resource_external_ip').
    """

    def decorator(resolver_func):
        """The actual decorator that registers the function."""
        if resolver_func not in _resolver_registry[lookup_key]:
            _resolver_registry[lookup_key].append(resolver_func)
        return resolver_func

    return decorator


def get_resource_ids(lookup_key: str, value: any) -> list[tuple[int, int]]:
    """
    Finds all resolvers for a given lookup key, executes them with the value,
    and returns an aggregated list of (content_type_id, object_id) tuples.

    Args:
        lookup_key: The key to look up in the registry (e.g., 'resource_external_ip').
        value: The value to pass to the resolver functions (e.g., '192.168.1.10').
    """
    all_resource_ids = []
    # .get() is used to safely handle cases where a key might not exist.
    for resolver in _resolver_registry.get(lookup_key, []):
        try:
            result = resolver(value)
            if result:
                all_resource_ids.extend(result)
        except Exception:
            logger.exception(
                "Resolver %s failed for key %s", resolver.__name__, lookup_key
            )
            pass
    return all_resource_ids


def filter_by_resource_attribute(queryset, name, value):
    """
    A single, generic method to handle any resource lookup via the registry.

    The 'name' argument (e.g., 'resource_name') is passed by django-filters
    and used directly as the lookup_key for our registry.
    """
    if not value:
        return queryset

    # Use the filter's name as the lookup key to find resource IDs
    resource_ids = get_resource_ids(lookup_key=name, value=value)

    if not resource_ids:
        return queryset.none()

    # Build the Q object from the list of (content_type_id, object_id) tuples
    q_objects = [
        Q(resource_content_type_id=ct_id, resource_object_id=obj_id)
        for ct_id, obj_id in resource_ids
    ]

    # Combine with OR and apply the filter
    combined_query = reduce(or_, q_objects)
    return queryset.filter(combined_query)
