from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace.models import (
    Attribute,
    AttributeOption,
    Category,
    Section,
)


def get_category_prefix(category):
    if category.sections.exists():
        # if at least one section exist, take its sections first prefix as category prefix
        return category.sections.first().key.split("_")[0]
    else:
        # cleanup whitespaces from the title
        return category.title.strip().replace(" ", "")


class Command(BaseCommand):
    help = "Copy structure of categories for the Marketplace"

    def add_arguments(self, parser):
        parser.add_argument(
            "source_category_uuid",
            nargs=1,
            type=str,
            help="UUID of a category to copy metadata from",
        )
        parser.add_argument(
            "target_category_uuid",
            nargs=1,
            type=str,
            help="UUID of a category to copy metadata to",
        )

    def handle(self, *args, **options):
        source_category_uuid = options["source_category_uuid"][0]
        target_category_uuid = options["target_category_uuid"][0]

        try:
            source_category = Category.objects.get(uuid=source_category_uuid)
        except Category.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(
                    "Source category %s was not found." % source_category_uuid
                )
            )
            exit(1)

        try:
            target_category = Category.objects.get(uuid=target_category_uuid)
        except Category.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(
                    "Target category %s was not found." % source_category_uuid
                )
            )
            exit(1)

        source_prefix = get_category_prefix(source_category)
        target_prefix = get_category_prefix(target_category)

        # Copy metadata
        for source_section in source_category.sections.all():
            section_source_prefix = source_section.key.split("_")[0]
            # assert that convention is respected
            if section_source_prefix != source_prefix:
                self.stdout.write(
                    self.style.ERROR(
                        f"Prefixes mismatch: {source_prefix} (from category) and {section_source_prefix} (from section)"
                    )
                )

            section_prefix = "_".join(
                [target_prefix] + source_section.key.split("_")[1:]
            )
            target_section, _ = Section.objects.get_or_create(
                key=section_prefix,
                title=source_section.title,
                category=target_category,
                is_standalone=source_section.is_standalone,
            )
            # copy attributes
            for source_attribute in source_section.attributes.all():
                attribute_target_key = (
                    target_prefix
                    + source_attribute.key[
                        source_attribute.key.find(source_prefix) + len(source_prefix) :
                    ]
                )
                attr, _ = Attribute.objects.get_or_create(
                    key=attribute_target_key,
                    title=source_attribute.title,
                    type=source_attribute.type,
                    section=target_section,
                )
                for source_option in source_attribute.options.all():
                    option_target_key = (
                        target_prefix
                        + source_option.key[
                            source_option.key.find(source_prefix) + len(source_prefix) :
                        ]
                    )
                    AttributeOption.objects.get_or_create(
                        attribute=attr,
                        key=option_target_key,
                        title=source_option.title,
                    )

        self.stdout.write(
            self.style.SUCCESS(
                "Target category %s was successfully populated." % target_category
            )
        )
