from django.core.management.base import BaseCommand

from waldur_core.core.metadata import WaldurConfiguration


class Command(BaseCommand):
    def handle(self, *args, **options):
        print('| **Name** | **Type** | **Description** | **Default value** |')
        print('| -------- | -------- | --------------- | ----------------- |')
        for (section_name, section) in WaldurConfiguration().__fields__.items():
            type_ = section.type_
            if hasattr(type_, '__fields__'):
                for field_name, field in type_.__fields__.items():
                    print(
                        f'| {section_name}.{field_name} | {field._type_display()} | {field.field_info.description} | {field.default} | '
                    )
            else:
                print(
                    f'| {section_name} | {section._type_display()} | {section.field_info.description} | {section.default} | '
                )
