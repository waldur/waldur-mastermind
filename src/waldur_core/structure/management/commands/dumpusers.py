from collections import OrderedDict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from waldur_core.structure import models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)

User = get_user_model()

USER_COLUMNS = OrderedDict(
    [
        # (Column name, User fields)
        ("Full name, Civil number", ("full_name", "civil_number")),
        ("Email, Phone nr.", ("email", "phone_number")),
        ("Job title", ("job_title",)),
        (
            "Staff, Support",
            (
                "is_staff",
                "is_support",
            ),
        ),
    ]
)

# in chars
COLUMN_MAX_WIDTH = 50


def format_string_to_column_size(string):
    if len(string) <= COLUMN_MAX_WIDTH:
        return string

    formatted = "\n".join(
        string[i : i + COLUMN_MAX_WIDTH]
        for i in range(0, len(string), COLUMN_MAX_WIDTH)
    )
    if isinstance(formatted, str):
        formatted = str(formatted, errors="replace")
    return formatted


def to_string(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, str):
        return format_string_to_column_size(value)
    elif isinstance(value, list):
        strings = [to_string(v) for v in value]
        result = ", ".join(strings)
        if len(result) > COLUMN_MAX_WIDTH:
            return "\n".join(strings)
        return result

    return format_string_to_column_size(str(value))


class Command(BaseCommand):
    help = "Dumps information about users, their organizations and projects."

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            default=None,
            help="Specifies file to which the output is written. The output will be printed to stdout by default.",
        )

    def handle(self, *args, **options):
        # fetch objects
        users = User.objects.all()

        # build table
        columns = list(USER_COLUMNS.keys()) + ["Organizations", "Projects"]
        c = columns
        dashes = "{:^25}+{:^30}+{:^30}+{:^5}+{:^25}+{:^25} \n".format(
            "-" * 25, "-" * 30, "-" * 30, "-" * 5, "-" * 25, "-" * 25
        )
        table = f"{c[0]:^25}|{c[1]:^30}|{c[2]:^30}|{c[3]:^5}|{c[4]:^25}|{c[5]:^25} \n"
        table += dashes
        for user in users:
            customers = models.Customer.objects.filter(
                id__in=get_connected_customers(user)
            ).values_list("name", flat=True)
            projects = models.Project.objects.filter(
                id__in=get_connected_projects(user)
            ).values_list("name", flat=True)
            row = [
                to_string(
                    [
                        getattr(user, f)
                        for f in fields
                        if getattr(user, f) not in ("", None)
                    ]
                )
                for fields in USER_COLUMNS.values()
            ] + [to_string(list(customers)), to_string(list(projects))]

            # row values split before and after comma into seperate rows
            split = [(s.split(",", 1)[0]) for s in row]
            addrow = [(s.split(",", 1)[-1]) for s in row]
            table += f"{split[0]:^25}|{split[1]:^30}|{split[2]:^30}|{split[3]:^5}|{split[4]:^25}|{split[5]:<25} \n"
            table += "{:^25}|{:^30}|{:^30}|{:^5}|{:^25}|{:<25} \n".format(*addrow)

            table += dashes

        # output
        if options["output"] is None:
            self.stdout.write(table)
        else:
            with open(options["output"], "w") as output_file:
                output_file.write(table)
