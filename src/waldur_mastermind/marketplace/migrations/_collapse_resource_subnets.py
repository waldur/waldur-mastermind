"""Pure planning logic for the 0258 per-resource -> (customer, offering) collapse.

Kept out of the migration module so it can be exercised directly: the migration
itself cannot be replayed in a test once ``ResourceAccessSubnet`` is deleted from
the app registry, but this function takes plain dicts and needs no models at all.
"""


def plan_collapse(rows):
    """Group per-resource access subnets by (customer, offering) and flag the effects.

    The collapse is a union, so a resource whose own list was narrower than a
    sibling's ends up reachable from more addresses than before. A resource that
    had no subnets at all was never concealed and starts inheriting the pair's
    list. Both effects are surfaced here so the migration can record them.

    ``rows`` is an iterable of dicts with the keys ``customer_id``,
    ``customer_name``, ``offering_id``, ``offering_name``, ``resource_name``,
    ``inet`` and ``description``. Pass ``inet=None`` for a resource that has no
    subnets.

    Returns a list of per-pair dicts sorted by customer name then offering name.
    """
    pairs = {}
    for row in rows:
        key = (row["customer_id"], row["offering_id"])
        pair = pairs.setdefault(
            key,
            {
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "offering_id": row["offering_id"],
                "offering_name": row["offering_name"],
                # inet -> description; the first description for an address wins,
                # which is why the resulting union is reported rather than
                # applied silently.
                "inets": {},
                "resources": {},
            },
        )
        own = pair["resources"].setdefault(row["resource_name"], set())
        if row["inet"] is None:
            continue
        inet = str(row["inet"])
        pair["inets"].setdefault(inet, row.get("description") or "")
        own.add(inet)

    report = []
    for pair in pairs.values():
        union = set(pair["inets"])
        widened = [
            {"resource_name": name, "gained": sorted(union - own)}
            for name, own in pair["resources"].items()
            if own and own != union
        ]
        newly_restricted = [
            name for name, own in pair["resources"].items() if not own and union
        ]
        report.append(
            {
                "customer_id": pair["customer_id"],
                "customer_name": pair["customer_name"],
                "offering_id": pair["offering_id"],
                "offering_name": pair["offering_name"],
                "inets": pair["inets"],
                "union": sorted(union),
                "resource_count": len(pair["resources"]),
                "widened": sorted(widened, key=lambda item: item["resource_name"]),
                "newly_restricted": sorted(newly_restricted),
            }
        )

    report.sort(key=lambda pair: (pair["customer_name"], pair["offering_name"]))
    return report
