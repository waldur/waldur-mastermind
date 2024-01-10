from django.contrib.admin.widgets import FilteredSelectMultiple


class ScrolledSelectMultiple(FilteredSelectMultiple):
    def __init__(self, verbose_name, is_stacked=False, attrs=None, choices=()):
        attrs = attrs or {"style": "overflow-x: auto"}
        super().__init__(verbose_name, is_stacked, attrs, choices)
