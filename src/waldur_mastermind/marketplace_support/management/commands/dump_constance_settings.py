from waldur_mastermind.marketplace_support.management.commands.dump_settings import (
    Command as DumpSettingsCommand,
)


class Command(DumpSettingsCommand):
    help = (
        "Former name of dump_settings, kept for deployments that call it.\n"
        + DumpSettingsCommand.help
    )
