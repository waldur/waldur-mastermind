from ddt import ddt, data
from rest_framework import status

from . import factories, base
from .. import models


@ddt
class CommentUpdateTest(base.BaseTest):

    def setUp(self):
        super(CommentUpdateTest, self).setUp()
        self.comment = factories.CommentFactory(issue=self.fixture.issue)
        self.url = factories.CommentFactory.get_url(self.comment)

    def test_staff_can_edit_comment(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.Comment.objects.filter(description=payload['description']).exists())

    @data('owner', 'admin', 'manager')
    def test_nonstaff_user_cannot_edit_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_valid_payload()

        response = self.client.patch(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Comment.objects.filter(description=payload['description']).exists())

    def _get_valid_payload(self):
        return {'description': 'New comment description'}


@ddt
class CommentDeleteTest(base.BaseTest):

    def setUp(self):
        super(CommentDeleteTest, self).setUp()
        self.comment = factories.CommentFactory(issue=self.fixture.issue)
        self.url = factories.CommentFactory.get_url(self.comment)

    def test_staff_can_delete_comment(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Comment.objects.filter(id=self.comment.id).exists())

    @data('owner', 'admin', 'manager')
    def test_nonstaff_user_cannot_delete_comment(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Comment.objects.filter(id=self.comment.id).exists())
