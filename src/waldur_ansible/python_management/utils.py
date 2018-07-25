from django.core.exceptions import ObjectDoesNotExist


def execute_safely(query):
    try:
        return query()
    except ObjectDoesNotExist:
        return None
