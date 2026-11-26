import hashlib
import json

from django.db import migrations

# Frozen rather than imported: a migration must not change when app code does.
K8S_CLUSTER_CONFIG_FIELD_TYPE = "k8s_cluster_config"
# The types k8s_cluster_config replaced, with the topology each implied when
# its option set no topology_mode.
LEGACY_TYPES = {
    "single_datacenter_k8s_config": "1-datacenter",
    "multi_datacenter_k8s_config": "3-datacenter",
}
OPTION_LISTS = ("options", "resource_options")


def rewrite_options(options, change):
    """``options`` with ``change`` applied to every option; None if nothing changed."""
    if not isinstance(options, dict):
        return None
    fields = options.get("options")
    if not isinstance(fields, dict):
        return None
    updated = {}
    for name, option in fields.items():
        new = change(option) if isinstance(option, dict) else None
        updated[name] = option if new is None else new
    if updated == fields:
        return None
    return {**options, "options": updated}


def policy_of(option):
    """A copy of the option's default_configs; {} when it is not a dict.

    Imported or synced offerings bypass the option serializer, so the stored
    value is not guaranteed to be one.
    """
    default_configs = option.get("default_configs")
    return dict(default_configs) if isinstance(default_configs, dict) else {}


def to_cluster_config(option):
    implied_topology = LEGACY_TYPES.get(option.get("type"))
    if implied_topology is None:
        return None
    default_configs = policy_of(option)
    # Keep the topologies the offering allowed: the old type decided them
    # when the provider had not. A blank mode counted as unset.
    if not default_configs.get("topology_mode"):
        default_configs["topology_mode"] = implied_topology
    return {
        **option,
        "type": K8S_CLUSTER_CONFIG_FIELD_TYPE,
        "default_configs": default_configs,
    }


def to_legacy_type(option):
    if option.get("type") != K8S_CLUSTER_CONFIG_FIELD_TYPE:
        return None
    default_configs = policy_of(option)
    # The new type lets the customer choose when nothing is set; the old types
    # need it spelled out.
    if not default_configs.get("topology_mode"):
        default_configs["topology_mode"] = "customer_choice"
    legacy_type = (
        "multi_datacenter_k8s_config"
        if default_configs["topology_mode"] == "3-datacenter"
        else "single_datacenter_k8s_config"
    )
    return {**option, "type": legacy_type, "default_configs": default_configs}


def document_hash(document):
    """The models' offering_template_document_hash, frozen."""
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def rewrite_document(document, change):
    """A template document with ``change`` applied to its forms; None if unchanged.

    The order form sits in offering.options, the resource options form in
    resource_options, each shaped like the offering's own fields.
    """
    if not isinstance(document, dict):
        return None
    updated = dict(document)
    offering = document.get("offering")
    if isinstance(offering, dict):
        new = rewrite_options(offering.get("options"), change)
        if new is not None:
            updated["offering"] = {**offering, "options": new}
    new = rewrite_options(document.get("resource_options"), change)
    if new is not None:
        updated["resource_options"] = new
    return None if updated == document else updated


def rewrite_offerings(apps, change):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in Offering.objects.only(*OPTION_LISTS).iterator():
        changed = []
        for field in OPTION_LISTS:
            new = rewrite_options(getattr(offering, field), change)
            if new is not None:
                setattr(offering, field, new)
                changed.append(field)
        if changed:
            offering.save(update_fields=changed)


def rewrite_templates(apps, change):
    # Written with update(), not save(): saving a template with a changed
    # document adds a version. Each stored version is rewritten in place
    # instead, with its hash kept in step.
    OfferingTemplate = apps.get_model("marketplace", "OfferingTemplate")
    for pk, document in OfferingTemplate.objects.values_list("pk", "document"):
        new = rewrite_document(document, change)
        if new is not None:
            OfferingTemplate.objects.filter(pk=pk).update(document=new)

    OfferingTemplateVersion = apps.get_model("marketplace", "OfferingTemplateVersion")
    for pk, document in OfferingTemplateVersion.objects.values_list("pk", "document"):
        new = rewrite_document(document, change)
        if new is not None:
            OfferingTemplateVersion.objects.filter(pk=pk).update(
                document=new, document_hash=document_hash(new)
            )


def forwards(apps, schema_editor):
    rewrite_offerings(apps, to_cluster_config)
    rewrite_templates(apps, to_cluster_config)


def backwards(apps, schema_editor):
    rewrite_offerings(apps, to_legacy_type)
    rewrite_templates(apps, to_legacy_type)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0337_offering_user_initial_password"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
