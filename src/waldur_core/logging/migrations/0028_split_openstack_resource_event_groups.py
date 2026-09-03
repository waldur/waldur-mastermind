from django.db import migrations

RESOURCES = "resources"
OPENSTACK_RESOURCES = "openstack_resources"

MODELS = [
    ("logging", "WebHook"),
    ("logging", "EmailHook"),
    ("logging", "SystemNotification"),
]


def add_openstack_resources(apps, schema_editor):
    """Keep every existing subscription delivering what it delivered before.

    OpenStack event types moved out of the generic `resources` group into
    `openstack_resources`, so a subscription naming only `resources` would
    otherwise silently narrow:

    - SystemNotification expands its groups on every dispatch
      (`BaseHook.all_event_types`), so it would stop delivering OpenStack
      events immediately;
    - a WebHook/EmailHook persists the expanded event types at write time and
      so keeps delivering them, until the next update through the API
      re-expands its groups and drops them.
    """
    for app_label, model_name in MODELS:
        model = apps.get_model(app_label, model_name)
        for row in model.objects.all().iterator():
            groups = row.event_groups or []
            if RESOURCES not in groups or OPENSTACK_RESOURCES in groups:
                continue
            row.event_groups = [*groups, OPENSTACK_RESOURCES]
            row.save(update_fields=["event_groups"])


def drop_openstack_resources(apps, schema_editor):
    """Deliberately a no-op.

    Reversing cannot tell the pairing this migration created from a
    subscription a user chose both groups for through the API, which is a
    supported flow. Since `resources` + `openstack_resources` delivers exactly
    what `resources` delivered before the split, leaving the extra group is
    harmless, while guessing would silently unsubscribe someone from OpenStack
    events they asked for.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("logging", "0027_emaillog_sent_at_index"),
    ]

    operations = [
        migrations.RunPython(add_openstack_resources, drop_openstack_resources),
    ]
