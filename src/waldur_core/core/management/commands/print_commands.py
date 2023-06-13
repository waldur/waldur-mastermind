from argparse import ArgumentParser

from django.core.management import get_commands, load_command_class
from django.core.management.base import BaseCommand

BLACK_LIST = [
    'print_commands',
    'print_settings',
    'print_features',
    'print_schema',
    'export_api_docs',
    'print_events',
    'print_templates',
]

WHITE_LIST = [
    'waldur',
    'axes',
]


class Command(BaseCommand):
    def handle(self, *args, **options):
        commands = []
        for name, path in get_commands().items():
            if not any(map(lambda x: x in path, WHITE_LIST)) or name in BLACK_LIST:
                continue
            command = load_command_class(path, name)
            commands.append((name, command))
        print('# CLI guide', end='\n\n')
        for name, command in sorted(commands, key=lambda x: x[0]):
            parser = ArgumentParser(prog=f'waldur {name}', add_help=False)
            command.add_arguments(parser)
            print('##', name)
            print()
            print(command.help.strip().replace('  ', ' '))
            print()
            if parser._actions:
                print('```bash')
                parser.print_help()
                print('```')
                print()
