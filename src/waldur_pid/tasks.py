import logging

from celery import shared_task
from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils

from . import backend, exceptions, mixins, models

logger = logging.getLogger(__name__)


@shared_task
def create_doi(serialized_instance):
    instance = core_utils.deserialize_instance(serialized_instance)

    if instance.datacite_doi:
        logger.warning(
            'Registration of %s has been skipped because datacite_doi field is not empty.'
            % instance
        )
        return

    try:
        backend.DataciteBackend().create_doi(instance)
    except exceptions.DataciteException as e:
        logger.critical(e)


@shared_task
def link_doi_with_collection(serialized_instance):
    instance = core_utils.deserialize_instance(serialized_instance)

    try:
        backend.DataciteBackend().link_doi_with_collection(instance)
    except exceptions.DataciteException as e:
        logger.critical(e)


@shared_task(name='waldur_pid.update_all_referrables')
def update_all_referrables():
    for model in mixins.DataciteMixin.get_all_models():
        for referrable in model.objects.exclude(datacite_doi=''):
            try:
                get_datacite_info_helper(referrable)
            except Exception as e:
                logger.critical(e)


@shared_task
def update_referrable(serialized_referrable):
    referrable = core_utils.deserialize_instance(serialized_referrable)
    try:
        logger.debug('Updating referrals of a Referrable %s.' % referrable)
        get_datacite_info_helper(referrable)
    except Exception as e:
        logger.critical(e)


def get_datacite_info_helper(referrable):
    logger.debug('Collecting referrals for Referrable %s' % referrable)
    doi = referrable.datacite_doi
    try:
        datacite_data = backend.DataciteBackend().get_datacite_data(doi)
    except exceptions.DataciteException as e:
        logger.warning("Failed to lookup metadata for a Referrable %s" % doi)
        logger.exception(e)
        return

    if datacite_data:
        content_type = ContentType.objects.get_for_model(referrable)
        referrable.citation_count = datacite_data['attributes']['citationCount']
        referrals_pids = [
            (x['relatedIdentifier'], x['relationType'], x['resourceTypeGeneral'],)
            for x in datacite_data['attributes']['relatedIdentifiers']
        ]
        for pid, rel_type, resource_type in referrals_pids:
            try:
                referral_attributes = backend.DataciteBackend().get_datacite_data(pid)[
                    'attributes'
                ]
            except exceptions.DataciteException as e:
                logger.warning("Failed to lookup metadata about a reference %s" % pid)
                logger.exception(e)
                continue

            # some assumptions to get a flat structure
            if len(referral_attributes['titles']) > 0:
                title = referral_attributes['titles'][0]['title']
            else:
                title = 'N/A'

            if len(referral_attributes['creators']) > 0:
                creator = referral_attributes['creators'][0]['name']
            else:
                creator = 'N/A'

            referrable_referral, _ = models.DataciteReferral.objects.update_or_create(
                pid=pid,
                content_type=content_type,
                object_id=referrable.id,
                defaults={
                    'relation_type': rel_type,
                    'resource_type': resource_type,
                    'creator': creator,
                    'publisher': referral_attributes['publisher'],
                    'title': title,
                    'published': referral_attributes['published'],
                    'referral_url': referral_attributes['url'],
                },
            )
        # cleanup stale citations
        models.DataciteReferral.objects.filter(
            content_type=content_type, object_id=referrable.id
        ).exclude(pid__in=[ref[0] for ref in referrals_pids]).delete()
        referrable.save(update_fields=['citation_count'])


@shared_task
def update_pid(serialized_referrable):
    referrable = core_utils.deserialize_instance(serialized_referrable)
    backend.DataciteBackend().update_doi(referrable)


@shared_task
def update_all_pid():
    for model in mixins.DataciteMixin.get_all_models():
        for referrable in model.objects.exclude(datacite_doi=''):
            serialized_referrable = core_utils.serialize_instance(referrable)
            update_pid.delay(serialized_referrable)
