import logging

from django.db import migrations, models

# ServiceProvider carried one column per account setting. They move into a
# single account_options JSON field, under the keys an offering's plugin
# options use and validated by the same serializer.
#
# Only values that set something are copied. A blank column meant "not set",
# and account_scope held the built-in "offering" for every provider that never
# chose, so copying it would read as a deliberate provider setting.
#
# Then the offerings drop the defaults they only stored by accident. The
# offering plugin-options serializer used to declare defaults for
# username_generation_policy, username_anonymized_prefix, homedir_prefix and
# login_shell, so every offering saved through the API stored them. An
# offering's value wins over its provider's, so a stored default silently hid
# any provider-level setting.
#
# A stored key is removed when it holds exactly its former default and the
# offering's service provider sets that setting, so that the offering inherits
# it. A key the provider does not set is kept: removing it would only swap one
# built-in default for another. Any other value was chosen and is kept.
#
# Username settings are removed only when that leaves the offering's generated
# usernames unchanged. Migrations run no signal handlers, so a policy or prefix
# change here would not regenerate usernames, and existing accounts would keep
# names the new setting no longer produces. Such offerings are logged; sending
# the key as a blank value through the API applies the provider setting and
# regenerates the usernames.
#
# Rows are changed with queryset updates. Reversing the cleanup is a noop: a
# removed key only ever held the built-in default.

logger = logging.getLogger(__name__)

COLUMNS = {
    "account_scope": "account_scope",
    "account_username_generation_policy": "username_generation_policy",
    "account_username_anonymized_prefix": "username_anonymized_prefix",
    "account_homedir_prefix": "homedir_prefix",
    "account_login_shell": "login_shell",
}
DEFAULT_SCOPE = "offering"

#: Setting -> the value the serializer default wrote.
STAMPED_DEFAULTS = {
    "username_generation_policy": "service_provider",
    "username_anonymized_prefix": "waldur_",
    "homedir_prefix": "/home/",
    "login_shell": "/bin/bash",
}
USERNAME_SETTINGS = ("username_generation_policy", "username_anonymized_prefix")
ANONYMIZED = "anonymized"


def copy_columns_to_options(apps, schema_editor):
    ServiceProvider = apps.get_model("marketplace", "ServiceProvider")
    for provider in ServiceProvider.objects.all().iterator():
        options = {}
        for column, key in COLUMNS.items():
            value = getattr(provider, column)
            if not value or (key == "account_scope" and value == DEFAULT_SCOPE):
                continue
            options[key] = value
        if options:
            ServiceProvider.objects.filter(pk=provider.pk).update(
                account_options=options
            )


def copy_options_to_columns(apps, schema_editor):
    ServiceProvider = apps.get_model("marketplace", "ServiceProvider")
    for provider in ServiceProvider.objects.exclude(account_options={}).iterator():
        options = provider.account_options or {}
        values = {column: options.get(key) or "" for column, key in COLUMNS.items()}
        values["account_scope"] = values["account_scope"] or DEFAULT_SCOPE
        ServiceProvider.objects.filter(pk=provider.pk).update(**values)


def username_settings(plugin_options: dict, provider_options: dict) -> tuple:
    """The resolved settings a generated username depends on."""
    resolved = {
        name: plugin_options.get(name)
        or provider_options.get(name)
        or STAMPED_DEFAULTS[name]
        for name in USERNAME_SETTINGS
    }
    policy = resolved["username_generation_policy"]
    if policy == ANONYMIZED:
        return policy, resolved["username_anonymized_prefix"]
    return policy, None


def removable_keys(plugin_options: dict, provider_options: dict) -> list[str]:
    """Stamped keys whose removal lets the provider setting apply safely."""
    stamped = [
        name
        for name, default in STAMPED_DEFAULTS.items()
        if plugin_options.get(name) == default and provider_options.get(name)
    ]
    remaining = {k: v for k, v in plugin_options.items() if k not in stamped}
    if username_settings(plugin_options, provider_options) != username_settings(
        remaining, provider_options
    ):
        stamped = [name for name in stamped if name not in USERNAME_SETTINGS]
    return stamped


def clear_stamped_account_settings(apps, schema_editor):
    ServiceProvider = apps.get_model("marketplace", "ServiceProvider")
    Offering = apps.get_model("marketplace", "Offering")
    providers = {
        provider.customer_id: provider.account_options
        for provider in ServiceProvider.objects.exclude(account_options={})
    }
    if not providers:
        return
    for offering in Offering.objects.filter(customer_id__in=providers).iterator():
        plugin_options = offering.plugin_options or {}
        provider_options = providers[offering.customer_id]
        keys = removable_keys(plugin_options, provider_options)
        kept = [
            name
            for name in USERNAME_SETTINGS
            if name not in keys
            and plugin_options.get(name) == STAMPED_DEFAULTS[name]
            and provider_options.get(name)
        ]
        if kept:
            logger.warning(
                "Offering %s keeps its stored %s: removing it would rename existing "
                "accounts. Send it as a blank value to apply the provider setting.",
                offering.uuid.hex,
                ", ".join(kept),
            )
        if not keys:
            continue
        Offering.objects.filter(pk=offering.pk).update(
            plugin_options={
                key: value for key, value in plugin_options.items() if key not in keys
            }
        )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0292_add_order_old_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="serviceprovider",
            name="account_options",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Account settings for this provider's offerings: account_scope, "
                    "username_generation_policy, username_anonymized_prefix, "
                    "homedir_prefix and login_shell. Each applies to every offering "
                    "that does not set the plugin option of the same name."
                ),
            ),
        ),
        migrations.RunPython(copy_columns_to_options, copy_options_to_columns),
        migrations.RemoveField(model_name="serviceprovider", name="account_scope"),
        migrations.RemoveField(
            model_name="serviceprovider", name="account_username_generation_policy"
        ),
        migrations.RemoveField(
            model_name="serviceprovider", name="account_username_anonymized_prefix"
        ),
        migrations.RemoveField(
            model_name="serviceprovider", name="account_homedir_prefix"
        ),
        migrations.RemoveField(
            model_name="serviceprovider", name="account_login_shell"
        ),
        migrations.RunPython(clear_stamped_account_settings, migrations.RunPython.noop),
    ]
