"""Context-aware confidence scoring for PII detections."""

# Words near a match that boost confidence (+0.15)
CONTEXT_BOOST_WORDS = {
    "pii_estonian_id": {"isikukood", "personal code", "id code", "identification"},
    "pii_iban_estonian": {"iban", "bank account", "transfer", "payment", "pangakonto"},
    "pii_iban_general": {"iban", "bank account", "transfer", "payment", "wire"},
    "pii_credit_card": {"card number", "credit card", "visa", "mastercard", "amex"},
    "pii_email": {"email", "e-mail", "contact", "send to"},
    "pii_phone_estonian": {"phone", "telefon", "call", "mobile", "mobiilinumber"},
    "pii_phone_e164": {"phone", "call", "mobile", "sms", "whatsapp"},
    "pii_private_key": {"ssh", "private key", "rsa", "deploy key"},
    "pii_password_context": {"login", "credential", "authenticate", "sign in"},
    "pii_database_url": {"database", "connection string", "db url", "migrate"},
    "pii_italy_codice_fiscale": {
        "codice fiscale",
        "tax code",
        "fiscal code",
        "italian id",
    },
    "pii_france_nir": {
        "nir",
        "sécurité sociale",
        "securite sociale",
        "social security",
        "numéro de sécurité",
    },
    "pii_finland_hetu": {
        "henkilötunnus",
        "hetu",
        "personal identity code",
        "finnish id",
    },
    "pii_spain_dni": {"dni", "documento nacional", "spanish id", "identidad"},
    "pii_spain_nie": {
        "nie",
        "número de identidad de extranjero",
        "foreigner id",
        "residencia",
    },
    "pii_poland_pesel": {"pesel", "personal number", "polish id", "numer pesel"},
    "pii_germany_steuer_id": {
        "steuer-id",
        "steueridentifikationsnummer",
        "steuerliche",
        "tax id",
        "identifikationsnummer",
    },
    "pii_czech_birth_number": {
        "rodné číslo",
        "rodne cislo",
        "birth number",
        "czech id",
    },
    "pii_netherlands_bsn": {
        "bsn",
        "burgerservicenummer",
        "citizen service number",
        "sofinummer",
    },
    "pii_sweden_personnummer": {
        "personnummer",
        "personal number",
        "swedish id",
        "personnr",
    },
    "pii_eu_vat": {
        "vat",
        "vat number",
        "tax number",
        "btw",
        "mwst",
        "iva",
        "tva",
    },
}

# Words that suppress confidence (-0.25) — examples, documentation, questions
CONTEXT_SUPPRESS_WORDS = frozenset(
    {
        "example",
        "sample",
        "format",
        "template",
        "test",
        "demo",
        "dummy",
        "fake",
        "placeholder",
        "what is",
        "how to",
        "looks like",
        "such as",
        "e.g.",
        "for instance",
        "documentation",
        "docs",
    }
)

# Known test/example values that should never trigger detection
KNOWN_TEST_VALUES = frozenset(
    {
        # Stripe test keys
        "sk_test_4eC39HqLyjWDarjtT1zdp7dc",
        "pk_test_TYooMQauvdEDq54NiTphI7jx",
        # AWS example key from docs
        "AKIAIOSFODNN7EXAMPLE",
        # Test credit card numbers
        "4111111111111111",
        "4111 1111 1111 1111",
        "5500000000000004",
        "378282246310005",
    }
)

# Email domains that indicate examples, not real PII
EXAMPLE_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "test.org",
        "localhost",
        "invalid",
        "noreply.com",
    }
)

# Context window: how many chars around the match to scan for boost/suppress words
_CONTEXT_WINDOW = 100


def calculate_pii_confidence(
    match_text: str,
    full_text: str,
    start: int,
    end: int,
    entity_type: str,
    has_checksum: bool,
    base_score: float,
    action_tier: str,
) -> float:
    score = base_score

    # Known test values → 0.0
    cleaned_match = match_text.strip()
    if cleaned_match in KNOWN_TEST_VALUES:
        return 0.0

    # Example email domains → 0.0
    if entity_type == "pii_email":
        domain = cleaned_match.rsplit("@", 1)[-1].lower()
        if domain in EXAMPLE_EMAIL_DOMAINS:
            return 0.0

    # Checksum validation boost
    if has_checksum:
        score += 0.15

    # Context window analysis — surrounding text ONLY (excluding the match itself)
    before = full_text[max(0, start - _CONTEXT_WINDOW) : start].lower()
    after = full_text[end : min(len(full_text), end + _CONTEXT_WINDOW)].lower()
    surrounding = before + " " + after

    # Check for boost words
    boost_words = CONTEXT_BOOST_WORDS.get(entity_type, set())
    for word in boost_words:
        if word in surrounding:
            score += 0.15
            break  # Only apply once

    # Check for suppress words — reduced penalty for BLOCK-tier high-confidence patterns
    suppress_penalty = (
        -0.10 if (action_tier == "block" and base_score >= 0.85) else -0.25
    )
    for word in CONTEXT_SUPPRESS_WORDS:
        if word in surrounding:
            score += suppress_penalty
            break  # Only apply once

    return max(0.0, min(1.0, score))
