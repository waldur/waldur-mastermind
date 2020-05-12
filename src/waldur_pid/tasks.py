import logging

from celery import shared_task

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

            offering_referral, _ = models.DataciteReferral.objects.update_or_create(
                pid=pid,
                scope=referrable,
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
        models.DataciteReferral.objects.filter(scope=referrable).exclude(
            pid__in=[ref[0] for ref in referrals_pids]
        ).delete()
        referrable.save(update_fields=['citation_count'])
