from upload_validator import FileTypeValidator

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


DocumentValidator = FileTypeValidator(
    allowed_types=[
        "text/csv",
        "text/html",
        "text/plain",
        "application/xhtml+xml",
        "application/pdf",
        "application/rtf",
        "application/msword",
        "application/vnd.ms-office",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
    ],
    allowed_extensions=[
        "csv",
        "txt",
        "htm",
        "html",
        "xhtml",
        "pdf",
        "rtf",
        "doc",
        "xls",
        "ppt",
        "docx",
        "xlsx",
        "pptx",
        "odt",
        "odp",
        "ods",
        "rtf",
    ],
)
