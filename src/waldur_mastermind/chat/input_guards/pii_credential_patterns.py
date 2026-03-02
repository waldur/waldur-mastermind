# PII & credential detection patterns.
# Tuple format: (regex_str, category, weight, display_name).
# Weights represent base confidence before context scoring adjusts them.
# display_name is the human-readable label shown to end users.

# Credentials that must never reach the LLM
BLOCK_PATTERNS = [
    # Private keys (PEM-encoded)
    (
        r"-----BEGIN\s+(RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE\s+KEY-----",
        "pii_private_key",
        0.95,
        "private key",
    ),
    # AWS Access Key ID (starts with AKIA)
    (
        r"\bAKIA[0-9A-Z]{16}\b",
        "pii_aws_access_key",
        0.90,
        "AWS access key",
    ),
    # AWS Secret Access Key (40 chars, base64-ish, requires keyword context)
    (
        r"(?i)(?:aws.?secret.?access.?key|secret.?key)\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}",
        "pii_aws_secret_key",
        0.90,
        "AWS secret key",
    ),
    # GCP API Key
    (
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        "pii_gcp_api_key",
        0.90,
        "GCP API key",
    ),
    # Azure subscription key / storage key (base64, 44+ chars with == ending, requires keyword context)
    (
        r"(?i)(?:azure|storage|account.?key|subscription.?key)\s*[=:]\s*[\"']?[A-Za-z0-9/+]{40,}==",
        "pii_azure_key",
        0.85,
        "Azure key",
    ),
    # GitHub personal access token (classic or fine-grained)
    (
        r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b",
        "pii_github_token",
        0.90,
        "GitHub token",
    ),
    # GitLab personal access token
    (
        r"\bglpat-[A-Za-z0-9\-]{20,}\b",
        "pii_gitlab_pat",
        0.90,
        "GitLab token",
    ),
    # Slack token (xoxb, xoxp, xoxo, xoxa, xoxs)
    (
        r"\bxox[bpoas]-[A-Za-z0-9\-]{10,}\b",
        "pii_slack_token",
        0.90,
        "Slack token",
    ),
    # Stripe secret/publishable key
    (
        r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}\b",
        "pii_stripe_key",
        0.90,
        "Stripe key",
    ),
    # Database connection URL with credentials
    (
        r"(postgres|mysql|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+",
        "pii_database_url",
        0.90,
        "database URL",
    ),
    # Password in assignment context (key=value, key:value)
    # Requires value to contain at least one non-alpha character (digit, symbol)
    # to exclude benign words like "reset_token", "management", "required"
    (
        r"(?i)(password|passwd|pwd|secret)\s*[=:]\s*(?!\*+\s)(?=\S*[^A-Za-z_\s]\S*)\S{8,}",
        "pii_password_context",
        0.75,
        "password",
    ),
    # SendGrid API key
    (
        r"\bSG\.[A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{22,}\b",
        "pii_sendgrid_key",
        0.90,
        "SendGrid key",
    ),
    # Slack webhook URL
    (
        r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+",
        "pii_slack_webhook",
        0.90,
        "Slack webhook",
    ),
]

# PII that should be masked
REDACT_PATTERNS = [
    # Estonian personal ID code (isikukood): 1-6 followed by 10 digits
    (
        r"\b[1-6]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{4}\b",
        "pii_estonian_id",
        0.50,
        "Estonian ID code",
    ),
    # Estonian IBAN (EE + 2 check digits + 16 digits)
    (
        r"\bEE\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b",
        "pii_iban_estonian",
        0.50,
        "Estonian IBAN",
    ),
    # General IBAN (2 letter country code + 2 check digits + up to 30 alphanumeric)
    (
        r"\b[A-Z]{2}\d{2}\s?[A-Z0-9]{4}(?:\s?[A-Z0-9]{4}){1,7}(?:\s?[A-Z0-9]{1,4})?\b",
        "pii_iban_general",
        0.45,
        "IBAN",
    ),
    # Credit card number (groups of 4 digits, with optional spaces/dashes)
    (
        r"\b(?:\d{4}[\s-]?){3}\d{4}\b",
        "pii_credit_card",
        0.50,
        "credit card number",
    ),
    # Estonian phone number (+372 followed by 7-8 digits)
    (
        r"\+372\s?\d{3,4}\s?\d{4}\b",
        "pii_phone_estonian",
        0.45,
        "Estonian phone number",
    ),
    # International phone (E.164 format)
    (
        r"\+[1-9]\d{6,14}\b",
        "pii_phone_e164",
        0.40,
        "phone number",
    ),
    # Italy Codice Fiscale: 6 letters + 2 digits + 1 month letter + 2 digits + 1 letter + 3 digits + check letter
    (
        r"(?<![A-Z0-9])[A-Z]{6}\d{2}[ABCDEHLMPRST]\d{2}[A-Z]\d{3}[A-Z](?![A-Z0-9])",
        "pii_italy_codice_fiscale",
        0.55,
        "Italian tax code",
    ),
    # France NIR (social security number): 13 significant digits + 2-digit key
    (
        r"(?<!\d)[12]\d{2}(?:0[1-9]|1[0-2])(?:2[AB]|\d{2})\d{6}(?:0[1-9]|[1-8]\d|9[0-7])(?!\d)",
        "pii_france_nir",
        0.55,
        "French social security number",
    ),
    # Finland HETU: DDMMYYCZZZQ
    (
        r"\b(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])\d{2}[+\-YXWVUABCDEF]\d{3}[0-9A-FHJK-NPR-Y]\b",
        "pii_finland_hetu",
        0.50,
        "Finnish personal ID",
    ),
    # Spain DNI: 8 digits + letter
    (
        r"(?<![A-Z0-9])\d{8}[\s\-]?[A-Z](?![A-Z0-9])",
        "pii_spain_dni",
        0.50,
        "Spanish DNI",
    ),
    # Spain NIE: X/Y/Z + 7 digits + letter
    (
        r"(?<![A-Z0-9])[XYZ][\s\-]?\d{7}[\s\-]?[A-Z](?![A-Z0-9])",
        "pii_spain_nie",
        0.50,
        "Spanish NIE",
    ),
    # Poland PESEL: 11 digits with structured month field
    (
        r"(?<!\d)\d{2}(?:0[1-9]|[12]\d|3[012]|[4-8]\d|9[012])\d{7}(?!\d)",
        "pii_poland_pesel",
        0.50,
        "Polish PESEL",
    ),
    # Germany Steuer-ID: 11 digits, first non-zero
    (
        r"(?<!\d)[1-9]\d{10}(?!\d)",
        "pii_germany_steuer_id",
        0.45,
        "German tax ID",
    ),
    # Czech birth number (Rodné číslo): YYMMDD/SSS(C)
    (
        r"\b\d{2}(?:0[1-9]|1[0-2]|2[1-9]|3[0-2]|5[1-9]|6[0-2]|7[1-9]|8[0-2])(?:0[1-9]|[12]\d|3[01])/?\d{3,4}\b",
        "pii_czech_birth_number",
        0.50,
        "Czech birth number",
    ),
    # Netherlands BSN: 8-9 digits.
    # WARNING: This pattern is intentionally broad (matches most 8-9 digit numbers).
    # False positives are controlled by three layers:
    #   1. elfproef validator (rejects ~91% of random numbers)
    #   2. Low base weight (0.40) — below the 0.60 REDACT threshold on its own
    #   3. Requires keyword context boost ("bsn", "burgerservicenummer", etc.) to reach threshold
    # Without keyword context, bare numbers like order IDs will NOT be detected.
    (
        r"(?<!\d)(?!000)\d{8,9}(?!\d)",
        "pii_netherlands_bsn",
        0.40,
        "Dutch BSN",
    ),
    # Sweden Personnummer: YYMMDD-NNNN (keyword-required via low base weight)
    (
        r"\b\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]|6[1-9]|[78]\d|9[01])[-+]?\d{4}\b",
        "pii_sweden_personnummer",
        0.40,
        "Swedish personal number",
    ),
]

# Possibly sensitive, context-dependent
WARN_PATTERNS = [
    # Email address (warn-only: emails are commonly shared in support chat)
    (
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "pii_email",
        0.50,
        "email address",
    ),
    # JWT token (three base64url-encoded segments separated by dots)
    (
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "pii_jwt",
        0.50,
        "JWT token",
    ),
    # Bearer token in header-like context
    (
        r"(?i)bearer\s+[A-Za-z0-9_\-.]{20,}",
        "pii_bearer_token",
        0.40,
        "bearer token",
    ),
    # Generic API key pattern (api_key=, apikey=, x-api-key:)
    (
        r"(?i)(api[_-]?key|x-api-key)\s*[=:]\s*[A-Za-z0-9_\-.]{16,}",
        "pii_generic_api_key",
        0.50,
        "API key",
    ),
    # EU VAT number (27 EU member states, country prefix is definitive)
    (
        r"\b(?:"
        r"ATU\d{8}"  # Austria
        r"|BE[01]\d{9}"  # Belgium
        r"|BG\d{9,10}"  # Bulgaria
        r"|HR\d{11}"  # Croatia
        r"|CY\d{8}[A-Z]"  # Cyprus
        r"|CZ\d{8,10}"  # Czechia
        r"|DK\d{8}"  # Denmark
        r"|EE\d{9}"  # Estonia
        r"|FI\d{8}"  # Finland
        r"|FR[A-HJ-NP-Z0-9]{2}\d{9}"  # France
        r"|DE\d{9}"  # Germany
        r"|EL\d{9}"  # Greece
        r"|HU\d{8}"  # Hungary
        r"|IE\d{7}[A-Z]{1,2}"  # Ireland
        r"|IT\d{11}"  # Italy
        r"|LV\d{11}"  # Latvia
        r"|LT\d{9,12}"  # Lithuania
        r"|LU\d{8}"  # Luxembourg
        r"|MT\d{8}"  # Malta
        r"|NL\d{9}B\d{2}"  # Netherlands
        r"|PL\d{10}"  # Poland
        r"|PT\d{9}"  # Portugal
        r"|RO\d{2,10}"  # Romania
        r"|SK\d{10}"  # Slovakia
        r"|SI\d{8}"  # Slovenia
        r"|ES[A-Z0-9]\d{7}[A-Z0-9]"  # Spain
        r"|SE\d{12}"  # Sweden
        r")\b",
        "pii_eu_vat",
        0.55,
        "EU VAT number",
    ),
]

ALL_PII_CREDENTIAL_PATTERNS = tuple(BLOCK_PATTERNS + REDACT_PATTERNS + WARN_PATTERNS)
