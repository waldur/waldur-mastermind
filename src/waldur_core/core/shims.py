import unicodedata

from django.contrib.auth import password_validation
from django.contrib.auth.hashers import (
    check_password, is_password_usable, make_password,
)
from django.db import models
from django.db.models.fields.related import lazy_related_operation
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _
from taggit.managers import TaggableManager as _TaggableManager


class AbstractBaseUser(models.Model):
    """
    is_active is removed
    """
    password = models.CharField(_('password'), max_length=128)
    last_login = models.DateTimeField(_('last login'), blank=True, null=True)

    REQUIRED_FIELDS = []

    # Stores the raw password if set_password() is called so that it can
    # be passed to password_changed() after the model is saved.
    _password = None

    class Meta:
        abstract = True

    def __str__(self):
        return self.get_username()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self._password is not None:
            password_validation.password_changed(self._password, self)
            self._password = None

    def get_username(self):
        """Return the username for this User."""
        return getattr(self, self.USERNAME_FIELD)

    def clean(self):
        setattr(self, self.USERNAME_FIELD, self.normalize_username(self.get_username()))

    def natural_key(self):
        return (self.get_username(),)

    @property
    def is_anonymous(self):
        """
        Always return False. This is a way of comparing User objects to
        anonymous users.
        """
        return False

    @property
    def is_authenticated(self):
        """
        Always return True. This is a way to tell if the user has been
        authenticated in templates.
        """
        return True

    def set_password(self, raw_password):
        self.password = make_password(raw_password)
        self._password = raw_password

    def check_password(self, raw_password):
        """
        Return a boolean of whether the raw_password was correct. Handles
        hashing formats behind the scenes.
        """
        def setter(raw_password):
            self.set_password(raw_password)
            # Password hash upgrades shouldn't be considered password changes.
            self._password = None
            self.save(update_fields=["password"])
        return check_password(raw_password, self.password, setter)

    def set_unusable_password(self):
        # Set a value that will never be a valid hash
        self.password = make_password(None)

    def has_usable_password(self):
        """
        Return False if set_unusable_password() has been called for this user.
        """
        return is_password_usable(self.password)

    def get_session_auth_hash(self):
        """
        Return an HMAC of the password field.
        """
        key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"
        return salted_hmac(key_salt, self.password).hexdigest()

    @classmethod
    def get_email_field_name(cls):
        try:
            return cls.EMAIL_FIELD
        except AttributeError:
            return 'email'

    @classmethod
    def normalize_username(cls, username):
        return unicodedata.normalize('NFKC', username) if isinstance(username, str) else username


class TaggableManager(_TaggableManager):
    """
    Modify contribute_to_class method, so it can use in abstract class
    See also: https://github.com/jazzband/django-taggit/pull/632/files
    """
    def contribute_to_class(self, cls, name):
        self.set_attributes_from_name(name)
        self.model = cls
        self.opts = cls._meta

        cls._meta.add_field(self)
        setattr(cls, name, self)
        if not cls._meta.abstract:
            self.remote_field.related_name = '%(class)s_%(model_name)s_%(app_label)s' % {
                "class": cls.__name__.lower(),
                "model_name": cls._meta.model_name.lower(),
                "app_label": cls._meta.app_label.lower(),
            }

            if self.remote_field.related_query_name:
                related_query_name = self.remote_field.related_query_name % {
                    "class": cls.__name__.lower(),
                    "app_label": cls._meta.app_label.lower(),
                }
                self.remote_field.related_query_name = related_query_name

            if isinstance(self.remote_field.model, str):

                def resolve_related_class(cls, model, field):
                    field.remote_field.model = model

                lazy_related_operation(
                    resolve_related_class, cls, self.remote_field.model, field=self
                )
            if isinstance(self.through, str):

                def resolve_related_class(cls, model, field):
                    self.through = model
                    self.remote_field.through = model
                    self.post_through_setup(cls)

                lazy_related_operation(
                    resolve_related_class, cls, self.through, field=self
                )
            else:
                self.post_through_setup(cls)
