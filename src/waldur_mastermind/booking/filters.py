from django_filters.rest_framework import DjangoFilterBackend


class OfferingCustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        return queryset
