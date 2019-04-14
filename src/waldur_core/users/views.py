from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import detail_route, list_route
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from waldur_core.core.views import ProtectedViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.users import models, filters, serializers, tasks
from waldur_core.users.utils import parse_invitation_token

User = get_user_model()


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
        sender = self.request.user.full_name or self.request.user.username
        if settings.WALDUR_CORE['ONLY_STAFF_CAN_INVITE_USERS'] and not self.request.user.is_staff:
            invitation.state = models.Invitation.State.REQUESTED
            invitation.save()
            transaction.on_commit(lambda: tasks.send_invitation_requested.delay(invitation.uuid.hex, sender))
        else:
            transaction.on_commit(lambda: tasks.process_invitation.delay(invitation.uuid.hex, sender))

    @list_route(methods=['post'], permission_classes=[])
    def approve(self, request):
        """
        For user's convenience invitation approval is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """
        token = request.data.get('token')
        if not token:
            raise ValidationError('token is required parameter')

        user, invitation = parse_invitation_token(token)
        invitation.approve(user)

        sender = ''
        if invitation.created_by:
            sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(lambda: tasks.process_invitation.delay(invitation.uuid.hex, sender))

        return Response({'detail': _('Invitation has been approved.')},
                        status=status.HTTP_200_OK)

    @list_route(methods=['post'], permission_classes=[])
    def reject(self, request):
        """
        For user's convenience invitation reject action is performed without authentication.
        User UUID and invitation UUID is encoded into cryptographically signed token.
        """
        token = request.data.get('token')
        if not token:
            raise ValidationError('token is required parameter')
        user, invitation = parse_invitation_token(token)
        invitation.reject()

        sender = ''
        if invitation.created_by:
            sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(lambda: tasks.send_invitation_rejected.delay(invitation.uuid.hex, sender))

        return Response({'detail': _('Invitation has been rejected.')},
                        status=status.HTTP_200_OK)

    @detail_route(methods=['post'])
    def send(self, request, uuid=None):
        invitation = self.get_object()

        if not self.can_manage_invitation_with(invitation.customer,
                                               invitation.customer_role,
                                               invitation.project_role):
            raise PermissionDenied()
        elif invitation.state not in (models.Invitation.State.PENDING, models.Invitation.State.EXPIRED):
            raise ValidationError(_('Only pending and expired invitations can be resent.'))

        if invitation.state == models.Invitation.State.EXPIRED:
            invitation.state = models.Invitation.State.PENDING
            invitation.created = timezone.now()
            invitation.save()

        sender = request.user.full_name or request.user.username
        tasks.send_invitation_created.delay(invitation.uuid.hex, sender)
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

        replace_email = False
        if invitation.email != request.user.email:
            if settings.WALDUR_CORE['VALIDATE_INVITATION_EMAIL']:
                raise ValidationError(_('Invitation and user emails mismatch.'))
            # Ensure that user wouldn't reuse existing email
            elif bool(request.data.get('replace_email')):
                if User.objects.filter(email=invitation.email).exists():
                    raise ValidationError(_('This email is already taken.'))
                else:
                    replace_email = True

        if settings.WALDUR_CORE['INVITATION_DISABLE_MULTIPLE_ROLES']:
            has_customer = structure_models.CustomerPermission.objects.filter(
                user=request.user, is_active=True).exists()
            has_project = structure_models.ProjectPermission.objects.filter(
                user=request.user, is_active=True).exists()
            if has_customer or has_project:
                raise ValidationError(_('User already has role within another customer or project.'))

        invitation.accept(request.user)
        if replace_email:
            request.user.email = invitation.email
            request.user.save(update_fields=['email'])

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
