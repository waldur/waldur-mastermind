import os

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

# max bytes to read for file type detection
READ_SIZE = 5 * (1024 * 1024)  # 5MB


@deconstructible
class FileTypeValidator:
    """
    File type validator for validating mimetypes and extensions

    Args:
        allowed_types (list): list of acceptable mimetypes e.g; ['image/jpeg', 'application/pdf']
                    see https://www.iana.org/assignments/media-types/media-types.xhtml
        allowed_extensions (list, optional): list of allowed file extensions e.g; ['.jpeg', '.pdf', '.docx']
    """

    type_message = _(
        "File type '%(detected_type)s' is not allowed. "
        "Allowed types are: '%(allowed_types)s'."
    )

    extension_message = _(
        "File extension '%(extension)s' is not allowed. "
        "Allowed extensions are: '%(allowed_extensions)s'."
    )

    invalid_message = _(
        "Allowed type '%(allowed_type)s' is not a valid type. "
        "See https://www.iana.org/assignments/media-types/media-types.xhtml"
    )

    def __init__(self, allowed_types, allowed_extensions=()):
        self.input_allowed_types = allowed_types
        self.allowed_mimes = self._normalize(allowed_types)
        self.allowed_exts = allowed_extensions

    def __call__(self, fileobj):
        import magic

        detected_type = magic.from_buffer(fileobj.read(READ_SIZE), mime=True)
        root, extension = os.path.splitext(fileobj.name.lower())

        # seek back to start so a valid file could be read
        # later without resetting the position
        fileobj.seek(0)

        if (
            detected_type not in self.allowed_mimes
            and detected_type.split("/")[0] not in self.allowed_mimes
        ):
            raise ValidationError(
                message=self.type_message,
                params={
                    "detected_type": detected_type,
                    "allowed_types": ", ".join(self.input_allowed_types),
                },
                code="invalid_type",
            )

        if self.allowed_exts and (extension not in self.allowed_exts):
            raise ValidationError(
                message=self.extension_message,
                params={
                    "extension": extension,
                    "allowed_extensions": ", ".join(self.allowed_exts),
                },
                code="invalid_extension",
            )

    def _normalize(self, allowed_types):
        """
        Validate and transforms given allowed types
        e.g; wildcard character specification will be normalized as text/* -> text
        """
        allowed_mimes = []
        for allowed_type in allowed_types:
            allowed_type = (
                allowed_type.decode()
                if isinstance(allowed_type, bytes)
                else allowed_type
            )
            parts = allowed_type.split("/")
            if len(parts) == 2:
                if parts[1] == "*":
                    allowed_mimes.append(parts[0])
                else:
                    allowed_mimes.append(allowed_type)
            else:
                raise ValidationError(
                    message=self.invalid_message,
                    params={"allowed_type": allowed_type},
                    code="invalid_input",
                )

        return allowed_mimes


ImageValidator = FileTypeValidator(
    allowed_types=[
        "image/png",
        "image/gif",
        "image/jpeg",
        "image/svg",
        "image/svg+xml",
        "image/x-icon",
    ]
)


CertificateValidator = FileTypeValidator(
    allowed_types=[
        "application/x-pem-file",
        "application/x-x509-ca-cert",
        "text/plain",
    ],
    allowed_extensions=["pem"],
)


def validate_notification_emails(value):
    if not value:
        return value
    try:
        value_str = str(value).strip()
        emails = []
        for email_str in value_str.split(","):
            email = email_str.strip()
            if email:
                emails.append(email)

        for email in emails:
            try:
                validate_email(email)
            except ValidationError:
                raise ValidationError(f"Invalid email address: {email}")

        return value

    except (TypeError, ValueError) as e:
        raise ValidationError(f"Invalid notification emails format: {e}")
