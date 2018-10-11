from django.contrib.admin.filters import (
    AllValuesFieldListFilter,
    ChoicesFieldListFilter,
    RelatedFieldListFilter, RelatedOnlyFieldListFilter
)


class DropdownFilter(AllValuesFieldListFilter):
    template = 'admin/core/dropdown_filter.html'


class ChoiceDropdownFilter(ChoicesFieldListFilter):
    template = 'admin/core/dropdown_filter.html'


class RelatedDropdownFilter(RelatedFieldListFilter):
    template = 'admin/core/dropdown_filter.html'


class RelatedOnlyDropdownFilter(RelatedOnlyFieldListFilter):
    template = 'admin/core/dropdown_filter.html'
