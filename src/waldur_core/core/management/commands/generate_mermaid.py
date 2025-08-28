# A Django Management Command to Generate Mermaid Class Diagrams
#
# This command introspects Django models from specified applications and
# generates a Mermaid Class Diagram definition. This allows for easy
# visualization of the data model, which can be used in documentation,
# wikis, or markdown files (like on GitHub).
#
# Inspired by the `graph_models` command from the `django-extensions` package.
#

import re

# --- Django Core Imports ---
from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.core.management.base import BaseCommand, CommandError
from django.db.models import (
    ForeignKey,
    ManyToManyField,
    OneToOneField,
)
from django.db.models.fields.reverse_related import (
    ManyToManyRel,
    ManyToOneRel,
    OneToOneRel,
)

# FIELD_TYPE_MAP provides a simple mapping from Django's field class names
# to more generic, commonly understood types used in diagrams.
# This helps simplify the output and make it more readable. It's not exhaustive
# but covers the most frequent field types. Unmapped types will use their
# Django class name.
FIELD_TYPE_MAP = {
    "AutoField": "int",
    "BigAutoField": "int",
    "BooleanField": "bool",
    "CharField": "string",
    "DateField": "date",
    "DateTimeField": "datetime",
    "DecimalField": "decimal",
    "EmailField": "string",
    "FloatField": "float",
    "IntegerField": "int",
    "PositiveIntegerField": "int",
    "PositiveSmallIntegerField": "int",
    "SlugField": "string",
    "SmallIntegerField": "int",
    "TextField": "string",
    "TimeField": "time",
    "URLField": "string",
    "UUIDField": "string",
    "FileField": "file",
    "ImageField": "image",
}


class MermaidGraph:
    """
    The main engine for generating the Mermaid diagram.

    This class handles the introspection of Django models, filtering them based
    on user-provided options, and constructing the final Mermaid diagram string.
    """

    def __init__(self, app_labels, **options):
        """
        Initializes the graph generator.

        Args:
            app_labels (list[str]): A list of app labels to introspect.
            **options (dict): A dictionary of command-line options that
                              control the diagram's output.
        """
        # Store the application labels provided by the user.
        self.app_labels = app_labels

        # Parse and store all the options from the command.
        # These will control various aspects of the diagram generation.
        self.include_models = self._parse_list_from_options(
            options.get("include_models")
        )
        self.exclude_models = self._parse_list_from_options(
            options.get("exclude_models")
        )
        self.exclude_field_types = self._parse_list_from_options(
            options.get("exclude_field_types")
        )
        self.verbose_names = options.get("verbose_names", False)
        self.inheritance = options.get("inheritance", True)
        self.direction = options.get("direction", "TB")
        self.show_fields = not options.get("disable_fields", False)

        # --- Initialization Workflow ---
        # 1. Get all models from the specified Django apps.
        self.all_models = self._get_all_models()
        # 2. Filter this list down to only the models we want to render,
        #    based on the include/exclude options.
        self.models_to_render = self._filter_models()

    def _parse_list_from_options(self, arg):
        """A simple helper to parse a comma-separated string into a list."""
        if not arg:
            return []
        return [e.strip() for e in arg.split(",")]

    def _get_all_models(self):
        """
        Retrieves all registered model classes from the specified applications.
        """
        all_models = []
        for app_label in self.app_labels:
            try:
                # Use Django's app registry to find the app configuration.
                app_config = apps.get_app_config(app_label)
                # Get all models associated with this app.
                all_models.extend(app_config.get_models())
            except LookupError:
                # If the app label is invalid, raise a CommandError for a clean exit.
                raise CommandError(f"App '{app_label}' not found.")
        return all_models

    def _use_model(self, model):
        """
        Determines if a given model should be included in the diagram based on
        the include and exclude lists. Wildcards ('*') are supported.
        """
        model_name = model._meta.object_name

        # The logic is as follows:
        # 1. If an include list is provided, a model MUST match it.
        # 2. If a model matches the include list (or if there is no include list),
        #    it must NOT match the exclude list.

        # Check against the include list first.
        if self.include_models:
            # We convert the wildcard pattern to a regex pattern.
            for pattern in self.include_models:
                # `re.match` checks for a match at the beginning of the string.
                # `$` ensures the whole string must match.
                if re.match(
                    f"^{pattern.replace('*', '.*')}$", model_name, re.IGNORECASE
                ):
                    # It matches the include list. Now check if it's excluded.
                    return not self._is_excluded(model_name)
            # If there's an include list and the model didn't match any pattern, exclude it.
            return False

        # If we get here, there was no include list.
        # So, we only need to check if the model is excluded.
        return not self._is_excluded(model_name)

    def _is_excluded(self, model_name):
        """Helper to check if a model name matches any pattern in the exclude list."""
        if self.exclude_models:
            for pattern in self.exclude_models:
                if re.match(
                    f"^{pattern.replace('*', '.*')}$", model_name, re.IGNORECASE
                ):
                    return True
        return False

    def _filter_models(self):
        """
        Applies the `_use_model` logic to the full list of models to get the
        final list of models that will be rendered in the diagram.
        """
        return [model for model in self.all_models if self._use_model(model)]

    def generate_diagram(self):
        """
        The main orchestration method.

        It generates the complete Mermaid diagram string by calling helper methods
        to create class definitions and relationship lines.
        """
        # Handle the case where no models match the criteria.
        if not self.models_to_render:
            return "classDiagram\n    %% No models found or matched the criteria"

        # Start building the output string.
        output = [
            "classDiagram",  # The required Mermaid header for a class diagram.
            f"    direction {self.direction}",  # Set the layout direction (e.g., Top to Bottom).
        ]

        class_definitions = []
        relationships = []

        # Iterate over each model that needs to be rendered.
        for model in self.models_to_render:
            # Generate the `class ModelName { ... }` block.
            class_definitions.append(self._get_model_definition(model))
            # Generate all relationship lines originating from this model.
            relationships.extend(self._get_model_relations(model))

        # Add the generated parts to the final output.
        output.extend(class_definitions)
        # Use a `set` to automatically remove any duplicate relationship lines
        # that might be generated, then sort for consistent output.
        output.extend(sorted(list(set(relationships))))

        return "\n".join(output)

    def _get_model_name(self, model):
        """
        Gets the display name for a model, respecting the `--verbose-names` flag.
        Replaces spaces with underscores for Mermaid syntax compatibility.
        """
        if self.verbose_names and hasattr(model._meta, "verbose_name"):
            return model._meta.verbose_name.replace(" ", "_")
        return model._meta.object_name

    def _get_field_name(self, field):
        """
        Gets the display name for a field, respecting the `--verbose-names` flag.
        """
        if self.verbose_names and hasattr(field, "verbose_name"):
            return field.verbose_name
        return field.name

    def _get_model_definition(self, model):
        """
        Generates the Mermaid `class` definition block for a single model,
        including all its fields.
        Example:
            class User {
                +int id [PK]
                +string username
            }
        """
        model_name = self._get_model_name(model)
        lines = [f"    class {model_name} {{"]

        # Only add fields if the user hasn't disabled them.
        if self.show_fields:
            # `model._meta.get_fields()` returns all fields and relationships
            # attached to a model, including forward, reverse, and M2M.
            for field in model._meta.get_fields():
                field_type_name = type(field).__name__

                # --- Field Filtering Logic ---
                # We must skip certain types of "fields" that aren't real columns
                # or are better represented as relationship arrows.
                if (
                    # Skip GenericForeignKey as it's a virtual field without attributes like .primary_key
                    isinstance(field, GenericForeignKey | GenericRelation)
                    or
                    # Skip all reverse relations (e.g., the 'post_set' on a User model).
                    # These are visualized by the forward relation arrow from the other model.
                    isinstance(field, ManyToOneRel | ManyToManyRel | OneToOneRel)
                    or
                    # Skip any field types the user explicitly requested to exclude.
                    # This is perfect for filtering out noisy fields like 'TranslationCharField'.
                    field_type_name in self.exclude_field_types
                ):
                    continue

                # Get a simplified type name from our map, or use the class name as a fallback.
                mermaid_type = FIELD_TYPE_MAP.get(field_type_name, field_type_name)
                field_name = self._get_field_name(field)

                # Add special markers for Primary and Foreign Keys for clarity.
                markers = []
                if field.primary_key:
                    markers.append("PK")
                if isinstance(field, ForeignKey):
                    markers.append("FK")
                marker_str = f" [{','.join(markers)}]" if markers else ""

                # --- Field Rendering Logic ---
                if isinstance(field, ForeignKey | OneToOneField | ManyToManyField):
                    # For relation fields, it's more informative to show the related model's
                    # name as the "type".
                    related_model_name = self._get_model_name(field.related_model)
                    lines.append(
                        f"        +{related_model_name} {field_name}{marker_str}"
                    )
                else:
                    # For all other standard fields.
                    lines.append(f"        +{mermaid_type} {field_name}{marker_str}")

        lines.append("    }")
        return "\n".join(lines)

    def _get_model_relations(self, model):
        """
        Generates all Mermaid relationship lines for a single model.
        Example:
            User "1" -- "*" Post : author
        """
        relations = []
        model_name = self._get_model_name(model)

        # Iterate over all fields to find the ones that define relationships.
        for field in model._meta.get_fields():
            # Check if the target model of the relation should be rendered. If not, skip this relation.
            if (
                hasattr(field, "related_model")
                and field.related_model
                and not self._use_model(field.related_model)
            ):
                continue

            # --- One-to-One Relationship ---
            if isinstance(field, OneToOneField):
                related_model_name = self._get_model_name(field.related_model)
                # Syntax: ModelA "1" -- "1" ModelB : field_name
                relations.append(
                    f'    {model_name} "1" -- "1" {related_model_name} : {field.name}'
                )

            # --- Foreign Key (Many-to-One) Relationship ---
            elif isinstance(field, ForeignKey):
                related_model_name = self._get_model_name(field.related_model)
                # Syntax: ManySide "*" -- "1" OneSide : foreign_key_field
                relations.append(
                    f'    {model_name} "*" -- "1" {related_model_name} : {field.name}'
                )

            # --- Many-to-Many Relationship ---
            elif isinstance(field, ManyToManyField):
                # To avoid drawing the M2M line twice, we only draw it from the
                # "owning" side of the relationship (the side where the ManyToManyField
                # is defined on an auto-created `through` model).
                if field.remote_field.through._meta.auto_created:
                    related_model_name = self._get_model_name(field.related_model)
                    # Syntax: ModelA "*" -- "*" ModelB : many_to_many_field
                    relations.append(
                        f'    {model_name} "*" -- "*" {related_model_name} : {field.name}'
                    )

        # --- Inheritance Relationship ---
        if self.inheritance:
            # `model.__bases__` gives the parent classes.
            for parent in model.__bases__:
                # We only care about parents that are also Django models and are included in our render list.
                if hasattr(parent, "_meta") and parent in self.models_to_render:
                    parent_name = self._get_model_name(parent)
                    # Mermaid syntax for inheritance is `<|--`
                    relations.append(f"    {parent_name} <|-- {model_name}")

        return relations


class Command(BaseCommand):
    """
    Defines the `generate_mermaid` management command that users will run.
    This class is responsible for parsing command-line arguments and orchestrating
    the diagram generation process via the `MermaidGraph` class.
    """

    # The help text displayed when running `manage.py generate_mermaid --help`
    help = "Generate a Mermaid Class Diagram for specified Django apps and models."

    def add_arguments(self, parser):
        """
        Define the command-line arguments this command accepts.
        """
        # Positional argument: one or more app labels (e.g., `auth`, `myapp`).
        parser.add_argument(
            "app_label", nargs="+", help="Name of the application or applications."
        )
        # Optional argument to save output to a file.
        parser.add_argument(
            "--output", "-o", dest="output_file", help="Save the diagram to a file."
        )
        # Optional argument to filter which models are included.
        parser.add_argument(
            "--include-models",
            "-i",
            dest="include_models",
            help="Models to include (comma-separated, wildcards supported).",
        )
        # Optional argument to filter which models are excluded.
        parser.add_argument(
            "--exclude-models",
            "-e",
            dest="exclude_models",
            help="Models to exclude (comma-separated, wildcards supported).",
        )
        # Optional argument to exclude specific field types by their class name.
        parser.add_argument(
            "--exclude-field-types",
            dest="exclude_field_types",
            help="Field class names to exclude (e.g., 'TranslationCharField,JsonField').",
        )
        # Optional flag to use verbose names for models and fields.
        parser.add_argument(
            "--verbose-names",
            action="store_true",
            help="Use model and field verbose_names.",
        )
        # Optional flag to disable inheritance arrows.
        parser.add_argument(
            "--no-inheritance",
            action="store_false",
            dest="inheritance",
            default=True,
            help="Don't draw inheritance arrows.",
        )
        # Optional argument to control the layout direction.
        parser.add_argument(
            "--direction",
            "-d",
            default="TB",
            choices=["TB", "BT", "LR", "RL"],
            help="Direction of the diagram layout.",
        )
        # Optional flag to hide all fields and only show model names and relationships.
        parser.add_argument(
            "--disable-fields",
            action="store_true",
            help="Don't show fields, only model names and relationships.",
        )

    def handle(self, *args, **options):
        """
        The main execution logic of the command.
        This method is called by Django when the command is run.
        """
        app_labels = options["app_label"]

        try:
            # 1. Instantiate the generator with the app labels and all CLI options.
            graph = MermaidGraph(app_labels, **options)
            # 2. Generate the final diagram string.
            mermaid_string = graph.generate_diagram()
        except CommandError as e:
            # If a known error occurs (like an invalid app name), display it cleanly.
            self.stderr.write(self.style.ERROR(str(e)))
            return

        # 3. Handle the output.
        if options["output_file"]:
            # If an output file is specified, write to it.
            output_file_path = options["output_file"]
            with open(output_file_path, "w") as f:
                f.write(mermaid_string)
            self.stdout.write(
                self.style.SUCCESS(f"Mermaid diagram saved to {output_file_path}")
            )
        else:
            # Otherwise, print the diagram to standard output (the console).
            self.stdout.write(mermaid_string)
