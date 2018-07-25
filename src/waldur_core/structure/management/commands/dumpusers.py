from collections import OrderedDict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
import prettytable
import six

from waldur_core.structure import models

User = get_user_model()

USER_COLUMNS = OrderedDict([
    # (Column name, User fields)
    ('Full name, Civil number', ('full_name', 'civil_number')),
    ('Email, Phone nr.', ('email', 'phone_number')),
    ('Job title', ('job_title',)),
    ('Staff, Support', ('is_staff', 'is_support',)),
])

# in chars
COLUMN_MAX_WIDTH = 25


def format_string_to_column_size(string):
    if len(string) <= COLUMN_MAX_WIDTH:
        return string

    formatted = '\n'.join(string[i:i + COLUMN_MAX_WIDTH] for i in range(0, len(string), COLUMN_MAX_WIDTH))
    if isinstance(formatted, str):
        formatted = six.text_type(formatted, errors='replace')
    return formatted


def to_string(value):
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, six.string_types):
        return format_string_to_column_size(value)
    elif isinstance(value, list):
        strings = [to_string(v) for v in value]
        result = ', '.join(strings)
        if len(result) > COLUMN_MAX_WIDTH:
            return '\n'.join(strings)
        return result

    return format_string_to_column_size(str(value))


class Command(BaseCommand):
    help = "Dumps information about users, their organizations and projects."

    def add_arguments(self, parser):
        parser.add_argument(
            '-o', '--output',
            dest='output', default=None,
            help='Specifies file to which the output is written. The output will be printed to stdout by default.',
        )

    def handle(self, *args, **options):
        # fetch objects
        users = User.objects.all()
        project_roles = models.ProjectPermission.objects.filter(is_active=True)
        customer_roles = models.CustomerPermission.objects.filter(is_active=True)

        # build table
        columns = list(USER_COLUMNS.keys()) + ['Organizations', 'Projects']
        table = prettytable.PrettyTable(columns, hrules=prettytable.ALL)
        for user in users:
            user_customers = to_string(list(customer_roles.filter(user=user)))
            user_projects = to_string(list(project_roles.filter(user=user)))
            row = [to_string([getattr(user, f) for f in fields if getattr(user, f) not in ('', None)])
                   for fields in USER_COLUMNS.values()]
            row += [user_customers, user_projects]
            table.add_row(row)

        # output
        if options['output'] is None:
            self.stdout.write(table.get_string())
            return

        with open(options['output'], 'w') as output_file:
            output_file.write(table.get_string())
