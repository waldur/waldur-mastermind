import yaml
from dbtemplates.models import Template
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Override templates"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            'templates_file',
            help='Specifies location of templates file.',
        )
        parser.add_argument(
            '-c',
            '--clean',
            dest='clean',
            default=False,
            help='This flag means total synchronization with the template file you pass.',
        )

    def handle(self, *args, **options):
        with open(options['templates_file']) as templates_file:
            templates = yaml.safe_load(templates_file)

        if templates is None:
            print("Templates file is empty.")
            return

        if options['clean']:
            all_templates = Template.objects.all()
            for template in all_templates:
                if template.name not in templates:
                    Template.objects.get(name=template.name).delete()

        for path, content in templates.items():
            Template.objects.filter(name=path).update(content=content)
