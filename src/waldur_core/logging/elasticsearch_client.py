from __future__ import unicode_literals

import logging

from django.conf import settings
from elasticsearch import Elasticsearch
import six

from waldur_core.core.utils import datetime_to_timestamp

logger = logging.getLogger(__name__)


class ElasticsearchError(Exception):
    pass


class ElasticsearchClientError(ElasticsearchError):
    pass


class ElasticsearchResultListError(ElasticsearchError):
    pass


class EmptyQueryset(object):
    def __len__(self):
        return 0

    def count(self):
        return 0

    def __getitem__(self, key):
        return []


class ElasticsearchResultList(object):
    """ List of results acceptable by django pagination """

    def __init__(self):
        self.client = ElasticsearchClient()

    def filter(self, should_terms=None, must_terms=None, must_not_terms=None, search_text='', start=None, end=None):
        setattr(self, 'total', None)
        self.client.prepare_search_body(
            should_terms=should_terms,
            must_terms=must_terms,
            must_not_terms=must_not_terms,
            search_text=search_text,
            start=start,
            end=end,
        )
        return self

    def order_by(self, sort):
        self.sort = sort
        return self

    def count(self):
        return self.client.get_count()

    def aggregated_count(self, ranges):
        return self.client.get_aggregated_by_timestamp_count(ranges)

    def _get_events(self, from_, size):
        return self.client.get_events(
            from_=from_,
            size=size,
            sort=getattr(self, 'sort', '-@timestamp'),
        )

    def __len__(self):
        if not hasattr(self, 'total') or self.total is None:
            self.total = self._get_events(0, 1)['total']
        return self.total

    def __getitem__(self, key):
        if isinstance(key, slice):
            if key.step is not None and key.step != 1:
                raise ElasticsearchResultListError('ElasticsearchResultList can be iterated only with step 1')
            start = key.start if key.start is not None else 0
            events_and_total = self._get_events(start, key.stop - start)
        else:
            events_and_total = self._get_events(key, 1)
        self.total = events_and_total['total']
        return events_and_total['events']


def _execute_if_not_empty(func):
    """ Execute function only if one of input parameters is not empty """
    def wrapper(*args, **kwargs):
        if any(args[1:]) or any(kwargs.items()):
            return func(*args, **kwargs)
    return wrapper


class ElasticsearchClient(object):

    class SearchBody(dict):
        FTS_FIELDS = (
            'message', 'customer_abbreviation', 'importance', 'project_group_name',
            'project_name', 'user_native_name', 'user_full_name')

        def __init__(self):
            self.queries = {}
            self.timestamp_filter = {}
            self.should_terms_filter = {}
            self.must_terms_filter = {}
            self.must_not_terms_filter = {}
            self.timestamp_ranges = []

        @_execute_if_not_empty
        def set_should_terms(self, terms):
            self.should_terms_filter.update(self.serialize_terms(terms))

        @_execute_if_not_empty
        def set_must_terms(self, terms):
            self.must_terms_filter.update(self.serialize_terms(terms))

        @_execute_if_not_empty
        def set_must_not_terms(self, terms):
            self.must_not_terms_filter.update(self.serialize_terms(terms))

        def serialize_terms(self, terms):
            result = {}
            for key, values in terms.items():
                result[key] = [six.text_type(value) for value in values]
            return result

        @_execute_if_not_empty
        def set_search_text(self, search_text):
            self.queries['search_text'] = ' OR '.join(
                [self._format_to_elasticsearch_field_filter(field, [search_text]) for field in self.FTS_FIELDS])

        @_execute_if_not_empty
        def set_timestamp_filter(self, start=None, end=None):
            if start is not None:
                self.timestamp_filter['gte'] = start.strftime('%Y-%m-%dT%H:%M:%S')
            if end is not None:
                self.timestamp_filter['lt'] = end.strftime('%Y-%m-%dT%H:%M:%S')

        @_execute_if_not_empty
        def set_timestamp_ranges(self, ranges):
            self.timestamp_ranges = []
            for r in ranges:
                timestamp_range = {}
                if 'start' in r:
                    timestamp_range['from'] = self.datetime_to_elasticsearch_timestamp(r['start'])
                if 'end' in r:
                    timestamp_range['to'] = self.datetime_to_elasticsearch_timestamp(r['end'])
                self.timestamp_ranges.append(timestamp_range)

        def prepare(self):
            # Valid event has event_type field
            self['query'] = {
                'bool': {
                    'must': [
                        {
                            'exists': {
                                'field': 'event_type'
                            }
                        }
                    ]
                }
            }

            if self.queries:
                self['query']['bool']['filter'] = {
                    'query_string': {
                        'query': ' AND '.join('(' + search_query + ')' for search_query in self.queries.values())
                    }
                }

            if self.should_terms_filter:
                self['query']['bool']['should'] = [
                    {'terms': {key: value}} for key, value in self.should_terms_filter.items()
                ]

            if self.must_terms_filter:
                self['query']['bool']['must'].extend([
                    {'terms': {key: value}} for key, value in self.must_terms_filter.items()
                ])

            if self.must_not_terms_filter:
                self['query']['bool']['must_not'] = [
                    {'terms': {key: value}} for key, value in self.must_not_terms_filter.items()
                ]

            if self.timestamp_filter:
                self['query']['bool']['must'].append({
                    'range': {'@timestamp': self.timestamp_filter}})

            if self.timestamp_ranges:
                self['aggs'] = {
                    'timestamp_ranges': {
                        'date_range': {
                            'field': '@timestamp',
                            'ranges': self.timestamp_ranges,
                        },
                    }
                }

        def datetime_to_elasticsearch_timestamp(self, dt):
            """ Elasticsearch calculates timestamp in milliseconds """
            return datetime_to_timestamp(dt) * 1000

        def _escape_elasticsearch_field_value(self, field_value):
            """
            Remove double quotes from field value

            Elasticsearch receives string query where all user input is strings in double quotes.
            But if input itself contains double quotes - elastic treat them as end of string, so we have to remove double
            quotes from search string.
            """
            return field_value.replace('\"', '')

        def _format_to_elasticsearch_field_filter(self, field_name, field_values):
            """
            Return string '<field_name>:("<field_value1>", "<field_value2>"...)'
            """
            excaped_field_values = [self._escape_elasticsearch_field_value(value) for value in field_values]
            return '%s:("%s")' % (field_name, '", "'.join(excaped_field_values))

    def __init__(self):
        self.client = self._get_client()

    def prepare_search_body(self, should_terms=None, must_terms=None, must_not_terms=None, search_text='', start=None, end=None):
        """
        Prepare body for elasticsearch query

        Search parameters
        ^^^^^^^^^^^^^^^^^
        These parameters are dictionaries and have format:  <term>: [<value 1>, <value 2> ...]
        should_terms: it resembles logical OR
        must_terms: it resembles logical AND
        must_not_terms: it resembles logical NOT

        search_text : string
            Text for FTS(full text search)
        start, end : datetime
            Filter for event creation time
        """
        self.body = self.SearchBody()
        self.body.set_should_terms(should_terms)
        self.body.set_must_terms(must_terms)
        self.body.set_must_not_terms(must_not_terms)
        self.body.set_search_text(search_text)
        self.body.set_timestamp_filter(start, end)
        self.body.prepare()

    def get_events(self, sort='-@timestamp', index='_all', from_=0, size=10, start=None, end=None):
        sort = sort[1:] + ':desc' if sort.startswith('-') else sort + ':asc'
        search_results = self.client.search(index=index, body=self.body, from_=from_, size=size, sort=sort)
        return {
            'events': [r['_source'] for r in search_results['hits']['hits']],
            'total': search_results['hits']['total'],
        }

    def get_count(self, index='_all'):
        count_results = self.client.count(index=index, body=self.body)
        return count_results['count']

    def get_aggregated_by_timestamp_count(self, ranges, index='_all'):
        self.body.set_timestamp_ranges(ranges)
        self.body.prepare()
        search_results = self.client.search(index=index, body=self.body, search_type='count')
        formatted_results = []
        for result in search_results['aggregations']['timestamp_ranges']['buckets']:
            formatted = {'count': result['doc_count']}
            if 'from' in result:
                # Divide by 1000 - because elasticsearch return return timestamp in microseconds
                formatted['start'] = result['from'] / 1000
            if 'to' in result:
                # Divide by 1000 - because elasticsearch return return timestamp in microseconds
                formatted['end'] = result['to'] / 1000
            formatted_results.append(formatted)
        return formatted_results

    def _get_elastisearch_settings(self):
        try:
            elasticsearch_settings = settings.WALDUR_CORE['ELASTICSEARCH']
        except (KeyError, AttributeError):
            raise ElasticsearchClientError(
                'Can not get elasticsearch settings. ELASTICSEARCH item in settings.WALDUR_CORE has '
                'to be defined.')

        required_configuration_fields = {'port', 'host', 'protocol'}
        if not required_configuration_fields.issubset(elasticsearch_settings):
            missing_fields = ','.join(required_configuration_fields - set(elasticsearch_settings))
            raise ElasticsearchClientError(
                'Following configuration items are missing in the "ELASTICSEARCH" section: %s' % missing_fields)

        empty_fields = [field for field in required_configuration_fields if not elasticsearch_settings[field]]
        if empty_fields:
            raise ElasticsearchClientError(
                'Following configuration items are empty in the "ELASTICSEARCH" section: %s' % empty_fields)

        return elasticsearch_settings

    def _get_client(self):
        elasticsearch_settings = self._get_elastisearch_settings()
        if elasticsearch_settings.get('username') and elasticsearch_settings.get('password'):
            path = '%(protocol)s://%(username)s:%(password)s@%(host)s:%(port)s' % elasticsearch_settings
        else:
            path = '%(protocol)s://%(host)s:%(port)s' % elasticsearch_settings
        client = Elasticsearch(
            [str(path)],
            verify_certs=elasticsearch_settings.get('verify_certs', False),
            ca_certs=elasticsearch_settings.get('ca_certs', ''),
        )
        # XXX Workaround for Python Elasticsearch client bugs
        if not elasticsearch_settings.get('verify_certs'):
            # Some parameters are handled incorrectly if verify_certs is false
            # Client's connection pool is the closes place we can fix this
            connection_pool = client.transport.get_connection().pool
            # If ca_certs is not set to 'None' explicitly it will be set to /etc/ssl/certs/ca-certificates.crt
            # which is missing on CentOS.
            # This bug only appears in RPM version of python-urrlib3 (v1.10.2-2 from CentOS Base):
            # http://mirror.centos.org/centos-7/7/os/x86_64/Packages/python-urllib3-1.10.2-2.el7_1.noarch.rpm
            # Upstream handles this situation correctly:
            # https://github.com/shazow/urllib3/blob/1.10.2/urllib3/connectionpool.py#L674L681
            connection_pool.ca_certs = None
            # If verify_certs is set to False no cert_reqs parameter is passed to urrlib3.HTTPSConnectionPool:
            # https://github.com/elastic/elasticsearch-py/blob/1.x/elasticsearch/connection/http_urllib3.py#L46L54
            # Somehow (I couldn't understand why) if cert_reqs is not set to ssl.CERT_NONE explicitly
            # certificate validation still happens -- and fails.
            # To work around the issue, cert_reqs is set to ssl.CERT_NONE explicitly.
            connection_pool.cert_reqs = 0  # ssl.CERT_NONE
        # XXX End of workaround
        return client
