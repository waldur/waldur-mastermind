import yaml
from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.management.base import BaseCommand
from django.db import models
from django.db.models.fields import Field
from django.db.models.fields.related import (
    ForeignKey,
    ManyToManyField,
    ManyToManyRel,
    OneToOneField,
)

WHITELIST_APPS = {
    "core",
    "marketplace",
    "permissions",
    "structure",
    "invoices",
    "users",
    "contenttypes",
}

BLACKLIST_FIELD_TYPES = {
    "TranslationCharField",
    "TranslationTextField",
}

FIELD_TYPE_MAPPING = {
    "AutoLastModified": "DateTime",
    "AutoCreated": "DateTime",
    "FSMInteger": "PositiveInteger",
    "Auto": "PositiveInteger",
}


class Command(BaseCommand):
    help = "Collect and export metadata about Django models"

    def handle(self, *args: any, **options: any) -> None:
        model_metadata: dict[str, dict[str, any]] = {}
        seen_tables = set()

        for app_config in apps.get_app_configs():
            for model in app_config.get_models():
                if model._meta.app_label not in WHITELIST_APPS:
                    continue
                if model._meta.db_table in seen_tables:
                    continue
                seen_tables.add(model._meta.db_table)
                model_info = self._get_model_metadata(model)
                if model_info:
                    model_metadata[model._meta.db_table] = model_info

                # Handle M2M through tables
                for field in model._meta.get_fields():
                    if not isinstance(field, ManyToManyField):
                        continue
                    model_metadata[field.remote_field.through._meta.db_table] = {
                        "columns": {
                            "id": {"type": "PositiveInteger"},
                            f"{field.m2m_column_name()}": {
                                "type": "ForeignKey",
                                "db_table": model._meta.db_table,
                            },
                            f"{field.m2m_reverse_name()}": {
                                "type": "ForeignKey",
                                "db_table": field.related_model._meta.db_table,
                            },
                        },
                        "type": "ManyToManyTable",
                    }

        print(
            yaml.dump(model_metadata, default_flow_style=False, allow_unicode=True),
            end="",
        )

    def _get_model_metadata(self, model: type[models.Model]) -> dict[str, any]:
        """
        Collect comprehensive metadata for a given model
        """
        model_info: dict[str, any] = {
            "columns": {},
            "app_label": model._meta.app_label,
            "model": model._meta.model_name,
            "type": "Table",
        }

        for field in model._meta.get_fields():
            if type(field).__name__ in BLACKLIST_FIELD_TYPES:
                continue
            if (
                getattr(field, "related_model", None)
                and field.related_model._meta.app_label not in WHITELIST_APPS
            ):
                continue
            match field:
                case field if hasattr(field, "field") and isinstance(
                    field.field, ForeignKey
                ):
                    continue
                case GenericForeignKey():
                    continue
                case ManyToManyField():
                    continue
                case ManyToManyRel():
                    continue

            field_info = self._get_field_metadata(field)
            if field_info:  # Only add if we got valid field info
                field_name = field.name
                if isinstance(field, ForeignKey | OneToOneField):
                    field_name = f"{field.name}_id"
                model_info["columns"][field_name] = field_info

        return model_info

    def _get_field_metadata(self, field: Field) -> dict[str, any]:
        """
        Extract detailed metadata for a specific field
        """
        field_type = type(field).__name__.replace("Field", "")
        field_type = FIELD_TYPE_MAPPING.get(field_type, field_type)

        field_info: dict[str, any] = {
            "type": field_type,
        }

        if isinstance(field, ForeignKey | OneToOneField):
            field_info["db_table"] = field.related_model._meta.db_table

        if (
            hasattr(field, "choices")
            and field.choices
            and field_type not in {"Char", "FSM"}
        ):
            field_info["choices"] = [
                {"value": value, "display": str(display).strip()}
                for (value, display) in field.choices
            ]

        return field_info
