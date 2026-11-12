from waldur_mastermind.marketplace_support.management.commands.override_settings import (
    Command as OverrideSettingsCommand,
)


class Command(OverrideSettingsCommand):
    help = (
        "Former name of override_settings, kept for deployments that call it.\n"
        + OverrideSettingsCommand.help
    )
