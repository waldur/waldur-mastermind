from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager


class Command(BaseCommand):
    help = """
    Manage demo data presets for Waldur.

    Available subcommands:
        list    - List all available presets
        info    - Show detailed information about a preset
        load    - Load a preset into the database
        export  - Export current database state as a preset

    Examples:
        waldur demo_presets list
        waldur demo_presets info minimal_quickstart
        waldur demo_presets load minimal_quickstart --dry-run
        waldur demo_presets load hpc_ai_platform
        waldur demo_presets export my_custom_preset --description "My setup"
    """

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(
            dest="subcommand",
            help="Available subcommands",
        )

        # List subcommand
        list_parser = subparsers.add_parser(
            "list",
            help="List all available demo presets",
        )
        list_parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            help="Output only preset names, one per line (useful for scripting)",
        )

        # Info subcommand
        info_parser = subparsers.add_parser(
            "info",
            help="Show detailed information about a preset",
        )
        info_parser.add_argument(
            "name",
            type=str,
            help="Name of the preset",
        )

        # Load subcommand
        load_parser = subparsers.add_parser(
            "load",
            help="Load a preset into the database",
        )
        load_parser.add_argument(
            "name",
            type=str,
            help="Name of the preset to load",
        )
        load_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without applying them",
        )
        load_parser.add_argument(
            "--no-cleanup",
            action="store_true",
            help="Skip cleanup of existing data before loading",
        )
        load_parser.add_argument(
            "--skip-users",
            action="store_true",
            help="Skip user import/cleanup",
        )
        load_parser.add_argument(
            "--skip-roles",
            action="store_true",
            help="Skip role import/cleanup",
        )
        load_parser.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="Skip confirmation prompt",
        )

        # Export subcommand
        export_parser = subparsers.add_parser(
            "export",
            help="Export current database state as a preset",
        )
        export_parser.add_argument(
            "name",
            type=str,
            help="Name for the new preset",
        )
        export_parser.add_argument(
            "-o",
            "--output",
            type=str,
            help="Custom output file path",
        )
        export_parser.add_argument(
            "-t",
            "--title",
            type=str,
            help="Human-readable title for the preset",
        )
        export_parser.add_argument(
            "-d",
            "--description",
            type=str,
            help="Description of the preset",
        )

    def handle(self, *args, **options):
        subcommand = options.get("subcommand")

        if not subcommand:
            self.print_help("manage.py", "demo_presets")
            return

        handler = getattr(self, f"handle_{subcommand}", None)
        if handler:
            handler(**options)
        else:
            raise CommandError(f"Unknown subcommand: {subcommand}")

    def handle_list(self, **options):
        """Handle the 'list' subcommand."""
        presets = DemoPresetManager.list_presets()
        quiet = options.get("quiet", False)

        if not presets:
            if not quiet:
                self.stdout.write(self.style.WARNING("No presets found."))
            return

        # Quiet mode: output only preset names, one per line
        if quiet:
            for preset in presets:
                self.stdout.write(preset.name)
            return

        self.stdout.write(self.style.SUCCESS("\nAvailable Demo Presets:"))
        self.stdout.write("=" * 60)

        for preset in presets:
            self.stdout.write(f"\n  {self.style.MIGRATE_HEADING(preset.name)}")
            self.stdout.write(f"    Title: {preset.title}")
            if preset.description:
                # Truncate long descriptions
                desc = preset.description
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                self.stdout.write(f"    Description: {desc}")
            self.stdout.write(f"    Version: {preset.version}")

            # Entity summary
            if preset.entity_counts:
                counts_str = ", ".join(
                    f"{k}: {v}" for k, v in preset.entity_counts.items() if v > 0
                )
                self.stdout.write(f"    Entities: {counts_str}")

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            f"\nTotal: {len(presets)} preset(s) available. "
            "Use 'demo_presets info <name>' for details."
        )

    def handle_info(self, **options):
        """Handle the 'info' subcommand."""
        name = options["name"]
        preset = DemoPresetManager.get_preset_info(name)

        if not preset:
            raise CommandError(f"Preset '{name}' not found")

        self.stdout.write(self.style.SUCCESS(f"\nPreset: {preset.name}"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"  Title:       {preset.title}")
        self.stdout.write(f"  Version:     {preset.version}")
        self.stdout.write(f"  Description: {preset.description}")

        if preset.scenarios:
            self.stdout.write("\n  Scenarios:")
            for scenario in preset.scenarios:
                self.stdout.write(f"    - {scenario}")

        if preset.entity_counts:
            self.stdout.write("\n  Entity Counts:")
            for entity, count in sorted(preset.entity_counts.items()):
                if count > 0:
                    self.stdout.write(f"    {entity}: {count}")

        self.stdout.write("\n" + "=" * 60)

    def handle_load(self, **options):
        """Handle the 'load' subcommand."""
        name = options["name"]
        dry_run = options.get("dry_run", False)
        cleanup_first = not options.get("no_cleanup", False)
        skip_users = options.get("skip_users", False)
        skip_roles = options.get("skip_roles", False)
        skip_confirmation = options.get("yes", False)

        # Verify preset exists
        preset = DemoPresetManager.get_preset_info(name)
        if not preset:
            raise CommandError(f"Preset '{name}' not found")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made\n")
            )

        self.stdout.write(f"Loading preset: {self.style.MIGRATE_HEADING(preset.title)}")
        self.stdout.write(f"  Version: {preset.version}")

        if cleanup_first:
            self.stdout.write(
                self.style.WARNING(
                    "\nWARNING: This will delete existing data before loading the preset!"
                )
            )

            # Ask for confirmation unless --yes flag is provided or dry-run mode
            if not skip_confirmation and not dry_run:
                confirm = input(
                    "\nAre you sure you want to proceed? Type 'yes' to confirm: "
                )
                if confirm.lower() != "yes":
                    self.stdout.write(self.style.ERROR("\nOperation cancelled."))
                    return

        self.stdout.write("")

        result = DemoPresetManager.load_preset(
            name=name,
            cleanup_first=cleanup_first,
            dry_run=dry_run,
            skip_users=skip_users,
            skip_roles=skip_roles,
        )

        if result.get("output"):
            self.stdout.write(result["output"])

        if result["success"]:
            self.stdout.write(self.style.SUCCESS(f"\n{result['message']}"))
        else:
            raise CommandError(result["message"])

    def handle_export(self, **options):
        """Handle the 'export' subcommand."""
        name = options["name"]
        output_path = options.get("output")
        title = options.get("title", "")
        description = options.get("description", "")

        self.stdout.write(f"Exporting current state as preset: {name}")

        result = DemoPresetManager.export_to_preset(
            name=name,
            title=title,
            description=description,
            output_path=output_path,
        )

        if result.get("output"):
            self.stdout.write(result["output"])

        if result["success"]:
            self.stdout.write(self.style.SUCCESS(f"\n{result['message']}"))
            if result.get("path"):
                self.stdout.write(f"  Path: {result['path']}")
        else:
            raise CommandError(result["message"])
