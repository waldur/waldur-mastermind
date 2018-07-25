import copy
import functools
import heapq
import itertools

from django.contrib.contenttypes.models import ContentType
from django.db import models
import six


class GenericKeyMixin(object):
    """
    Filtering by generic key field

    Support filtering by:
     - generic key directly: <generic_key_name>=<value>
     - is generic key null: <generic_key_name>__isnull=True|False
    """

    def __init__(
            self, generic_key_field='scope',
            object_id_field='object_id', content_type_field='content_type', available_models=(), **kwargs):
        super(GenericKeyMixin, self).__init__(**kwargs)
        self.generic_key_field = generic_key_field
        self.object_id_field = object_id_field
        self.content_type_field = content_type_field
        self.available_models = available_models

    def _preprocess_kwargs(self, initial_kwargs):
        """ Replace generic key related attribute with filters by object_id and content_type fields """
        kwargs = initial_kwargs.copy()
        generic_key_related_kwargs = self._get_generic_key_related_kwargs(initial_kwargs)
        for key, value in generic_key_related_kwargs.items():
            # delete old kwarg that was related to generic key
            del kwargs[key]
            try:
                suffix = key.split('__')[1]
            except IndexError:
                suffix = None
            # add new kwargs that related to object_id and content_type fields
            new_kwargs = self._get_filter_object_id_and_content_type_filter_kwargs(value, suffix)
            kwargs.update(new_kwargs)

        return kwargs

    def _get_generic_key_related_kwargs(self, initial_kwargs):
        # Skip fields like scope_customer
        return {key: value for key, value in initial_kwargs.items()
                if key.startswith(self.generic_key_field + '__') or key == self.generic_key_field}

    def _get_filter_object_id_and_content_type_filter_kwargs(self, generic_key_value, suffix=None):
        kwargs = {}
        if suffix is None:
            kwargs[self.object_id_field] = generic_key_value.id
            generic_key_content_type = ContentType.objects.get_for_model(generic_key_value)
            kwargs[self.content_type_field] = generic_key_content_type
        elif suffix == 'in':
            kwargs[self.object_id_field + '__in'] = [obj.id for obj in generic_key_value]
            kwargs[self.content_type_field] = ContentType.objects.get_for_model(generic_key_value[0])
        elif suffix == 'isnull':
            kwargs[self.object_id_field + '__isnull'] = generic_key_value
            kwargs[self.content_type_field + '__isnull'] = generic_key_value
        return kwargs

    def filter(self, *args, **kwargs):
        kwargs = self._preprocess_kwargs(kwargs)
        return super(GenericKeyMixin, self).filter(*args, **kwargs)

    def get(self, *args, **kwargs):
        kwargs = self._preprocess_kwargs(kwargs)
        return super(GenericKeyMixin, self).get(*args, **kwargs)

    def get_or_create(self, *args, **kwargs):
        kwargs = self._preprocess_kwargs(kwargs)
        return super(GenericKeyMixin, self).get_or_create(*args, **kwargs)


class SummaryQuerySet(object):
    """ Fake queryset that emulates union of different models querysets """

    def __init__(self, summary_models):
        self.querysets = [model.objects.all() for model in summary_models]
        self._order_by = None

    def filter(self, *args, **kwargs):
        self.querysets = [qs.filter(*copy.deepcopy(args), **copy.deepcopy(kwargs)) for qs in self.querysets]
        return self

    def exclude(self, *args, **kwargs):
        self.querysets = [qs.exclude(*copy.deepcopy(args), **copy.deepcopy(kwargs)) for qs in self.querysets]
        return self

    def distinct(self, *args, **kwargs):
        self.querysets = [qs.distinct(*copy.deepcopy(args), **copy.deepcopy(kwargs)) for qs in self.querysets]
        return self

    def order_by(self, order_by):
        self._order_by = order_by
        self.querysets = [qs.order_by(copy.deepcopy(order_by)) for qs in self.querysets]
        return self

    def count(self):
        return sum([qs.count() for qs in self.querysets])

    def all(self):
        return self

    def none(self):
        try:
            return self.querysets[0].none()
        except IndexError:
            return

    def __getitem__(self, val):
        chained_querysets = self._get_chained_querysets()
        if isinstance(val, slice):
            return list(itertools.islice(chained_querysets, val.start, val.stop))
        else:
            try:
                return next(itertools.islice(chained_querysets, val, val + 1))
            except StopIteration:
                raise IndexError

    def __len__(self):
        return sum([q.count() for q in self.querysets])

    def _get_chained_querysets(self):
        if self._order_by:
            return self._merge([qs.iterator() for qs in self.querysets], compared_attr=self._order_by)
        else:
            return itertools.chain(*[qs.iterator() for qs in self.querysets])

    def _merge(self, subsequences, compared_attr='pk'):

        @functools.total_ordering
        class Compared(object):
            """ Order objects by their attributes, reverse ordering if <reverse> is True """

            def __init__(self, obj, attr, reverse=False):
                self.attr = functools.reduce(Compared.get_obj_attr, attr.split("__"), obj)
                if isinstance(self.attr, six.string_types):
                    self.attr = self.attr.lower()
                self.reverse = reverse

            @staticmethod
            def get_obj_attr(obj, attr):
                # for m2m relationship support - get first instance of manager.
                # for example: get first project group if resource has to be ordered by project groups.
                if isinstance(obj, models.Manager):
                    obj = obj.first()
                return getattr(obj, attr) if obj else None

            def __eq__(self, other):
                return self.attr == other.attr

            def __le__(self, other):
                # In MySQL NULL values come *first* with ascending sort order.
                # We use the same behaviour.
                if self.attr is None:
                    return not self.reverse
                elif other.attr is None:
                    return self.reverse
                else:
                    return self.attr < other.attr if not self.reverse else self.attr >= other.attr

        reverse = compared_attr.startswith('-')
        if reverse:
            compared_attr = compared_attr[1:]

        # prepare a heap whose items are
        # (compared, current-value, iterator), one each per (non-empty) subsequence
        # <compared> is used for model instances comparison based on given attribute
        heap = []
        for subseq in subsequences:
            iterator = iter(subseq)
            for current_value in iterator:
                # subseq is not empty, therefore add this subseq's item to the list
                heapq.heappush(
                    heap, (Compared(current_value, compared_attr, reverse=reverse), current_value, iterator))
                break

        while heap:
            # get and yield lowest current value (and corresponding iterator)
            _, current_value, iterator = heap[0]
            yield current_value
            for current_value in iterator:
                # subseq is not finished, therefore add this subseq's item back into the priority queue
                heapq.heapreplace(
                    heap, (Compared(current_value, compared_attr, reverse=reverse), current_value, iterator))
                break
            else:
                # subseq has been exhausted, therefore remove it from the queue
                heapq.heappop(heap)
