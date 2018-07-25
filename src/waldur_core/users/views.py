from django.conf import settings
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import detail_route
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_core.core.views import ProtectedViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.users import models, filters, serializers, tasks


class InvitationViewSet(ProtectedViewSet):
    queryset = models.Invitation.objects.all().order_by('-created')
    serializer_class = serializers.InvitationSerializer
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.InvitationCustomerFilterBackend,
    )
    filter_class = filters.InvitationFilter
    lookup_field = 'uuid'

    def can_manage_invitation_with(self, customer, customer_role=None, project_role=None):
        user = self.request.user
        if user.is_staff:
            return True

        is_owner = customer.has_user(user, structure_models.CustomerRole.OWNER)
        can_manage_owners = settings.WALDUR_CORE['OWNERS_CAN_MANAGE_OWNERS']

        # It is assumed that either customer_role or project_role is not None
        if customer_role:
            return is_owner and can_manage_owners
        if project_role:
            return is_owner

    def perform_create(self, serializer):
        project = serializer.validated_data.get('project')
        if project:
            customer = project.customer
        else:
            customer = serializer.validated_data.get('customer')

        customer_role = serializer.validated_data.get('customer_role')
        project_role = serializer.validated_data.get('project_role')

        if not self.can_manage_invitation_with(customer, customer_role, project_role):
            raise PermissionDenied()

        invitation = serializer.save()
        tasks.send_invitation.delay(invitation.uuid.hex, self.request.user.full_name or self.request.user.username)

    @detail_route(methods=['post'])
    def send(self, request, uuid=None):
        invitation = self.get_object()

        if not self.can_manage_invitation_with(invitation.customer,
                                               invitation.customer_role,
                                               invitation.project_role):
            raise PermissionDenied()
        elif invitation.state == models.Invitation.State.ACCEPTED or \
                invitation.state == models.Invitation.State.CANCELED:
            raise ValidationError(_('Only pending and expired invitations can be resent.'))

        if invitation.state == models.Invitation.State.EXPIRED:
            invitation.state = models.Invitation.State.PENDING
            invitation.created = timezone.now()
            invitation.save()

        tasks.send_invitation.delay(invitation.uuid.hex, self.request.user.full_name or self.request.user.username)
        return Response({'detail': _('Invitation sending has been successfully scheduled.')},
                        status=status.HTTP_200_OK)

    @detail_route(methods=['post'])
    def cancel(self, request, uuid=None):
        invitation = self.get_object()

        if not self.can_manage_invitation_with(invitation.customer,
                                               invitation.customer_role,
                                               invitation.project_role):
            raise PermissionDenied()
        elif invitation.state != models.Invitation.State.PENDING:
            raise ValidationError(_('Only pending invitation can be canceled.'))

        invitation.cancel()
        return Response({'detail': _('Invitation has been successfully canceled.')},
                        status=status.HTTP_200_OK)

    @detail_route(methods=['post'], filter_backends=[])
    def accept(self, request, uuid=None):
        """ Accept invitation for current user.

            To replace user's email with email from invitation - add parameter
            'replace_email' to request POST body.
        """
        invitation = self.get_object()

        if invitation.state != models.Invitation.State.PENDING:
            raise ValidationError(_('Only pending invitation can be accepted.'))
        elif invitation.civil_number and invitation.civil_number != request.user.civil_number:
            raise ValidationError(_('User has an invalid civil number.'))

        if invitation.project:
            if invitation.project.has_user(request.user):
                raise ValidationError(_('User already has role within this project.'))
        elif invitation.customer.has_user(request.user):
            raise ValidationError(_('User already has role within this customer.'))

        if settings.WALDUR_CORE['VALIDATE_INVITATION_EMAIL'] and invitation.email != request.user.email:
            raise ValidationError(_('Invitation and user emails mismatch.'))

        replace_email = bool(request.data.get('replace_email'))
        invitation.accept(request.user, replace_email=replace_email)
        return Response({'detail': _('Invitation has been successfully accepted.')},
                        status=status.HTTP_200_OK)

    @detail_route(methods=['post'], filter_backends=[], permission_classes=[])
    def check(self, request, uuid=None):
        invitation = self.get_object()

        if invitation.state != models.Invitation.State.PENDING:
            return Response(status=status.HTTP_404_NOT_FOUND)
        elif invitation.civil_number:
            return Response({'email': invitation.email, 'civil_number_required': True}, status=status.HTTP_200_OK)
        else:
            return Response({'email': invitation.email}, status=status.HTTP_200_OK)
