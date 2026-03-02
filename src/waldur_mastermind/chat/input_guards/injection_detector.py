import logging
import re
import unicodedata

from constance import config

from waldur_mastermind.chat.input_guards.base import (
    BaseDetector,
    DetectionAction,
    InjectionResult,
    SeverityLevel,
)
from waldur_mastermind.chat.input_guards.injection_patterns import ALL_PATTERNS

logger = logging.getLogger(__name__)


# Mapping of common confusable Unicode chars to Latin ASCII equivalents.
# Covers Cyrillic, Greek, and Armenian homoglyphs that NFKC normalization does NOT handle.
_CONFUSABLE_MAP = str.maketrans(
    {
        # --- Cyrillic lowercase → Latin ---
        "\u0430": "a",  # Cyrillic а → Latin a
        "\u0435": "e",  # Cyrillic е → Latin e
        "\u043e": "o",  # Cyrillic о → Latin o
        "\u0440": "p",  # Cyrillic р → Latin p
        "\u0441": "c",  # Cyrillic с → Latin c
        "\u0443": "y",  # Cyrillic у → Latin y (visual)
        "\u0456": "i",  # Cyrillic і → Latin i
        "\u0455": "s",  # Cyrillic ѕ → Latin s
        "\u04bb": "h",  # Cyrillic һ → Latin h
        "\u043a": "k",  # Cyrillic ка → Latin k
        "\u043d": "n",  # Cyrillic н → Latin n (visual)
        "\u043c": "m",  # Cyrillic м → Latin m (visual)
        "\u0445": "x",  # Cyrillic х → Latin x
        "\u0432": "v",  # Cyrillic в → Latin v (visual)
        "\u0442": "t",  # Cyrillic т → Latin t
        # --- Cyrillic uppercase → Latin ---
        "\u0410": "A",  # Cyrillic А → Latin A
        "\u0412": "B",  # Cyrillic В → Latin B
        "\u0415": "E",  # Cyrillic Е → Latin E
        "\u041a": "K",  # Cyrillic К → Latin K
        "\u041c": "M",  # Cyrillic М → Latin M
        "\u041d": "H",  # Cyrillic Н → Latin H
        "\u041e": "O",  # Cyrillic О → Latin O
        "\u0420": "P",  # Cyrillic Р → Latin P
        "\u0421": "C",  # Cyrillic С → Latin C
        "\u0422": "T",  # Cyrillic Т → Latin T
        "\u0425": "X",  # Cyrillic Х → Latin X
        # --- Greek lowercase → Latin ---
        "\u03b1": "a",  # Greek α (alpha)
        "\u03bf": "o",  # Greek ο (omicron)
        "\u03c1": "p",  # Greek ρ (rho)
        "\u03b5": "e",  # Greek ε (epsilon)
        "\u03b9": "i",  # Greek ι (iota)
        "\u03bd": "v",  # Greek ν (nu, visual v)
        "\u03ba": "k",  # Greek κ (kappa)
        "\u03c4": "t",  # Greek τ (tau, visual)
        "\u03c5": "u",  # Greek υ (upsilon)
        # --- Greek uppercase → Latin ---
        "\u0391": "A",  # Α (Alpha)
        "\u0392": "B",  # Β (Beta)
        "\u0395": "E",  # Ε (Epsilon)
        "\u0396": "Z",  # Ζ (Zeta)
        "\u0397": "H",  # Η (Eta)
        "\u0399": "I",  # Ι (Iota)
        "\u039a": "K",  # Κ (Kappa)
        "\u039c": "M",  # Μ (Mu)
        "\u039d": "N",  # Ν (Nu)
        "\u039f": "O",  # Ο (Omicron)
        "\u03a1": "P",  # Ρ (Rho)
        "\u03a4": "T",  # Τ (Tau)
        "\u03a5": "Y",  # Υ (Upsilon)
        "\u03a7": "X",  # Χ (Chi)
        # --- Armenian (common homoglyphs) ---
        "\u0585": "o",  # Armenian oh → o
        "\u057d": "s",  # Armenian seh → s
        "\u0561": "a",  # Armenian ayb → a (visual)
        "\u0570": "h",  # Armenian ho → h
        "\u0578": "n",  # Armenian now → n
    }
)

# Unicode categories whose characters are invisible and should be stripped
# for injection pattern matching: Cf (Format), Mn (Nonspacing Mark), Me (Enclosing Mark)
_STRIP_CATEGORIES = frozenset({"Cf", "Mn", "Me"})

# Space lookalikes that are NOT matched by \s (category So/Lo, not stripped by _STRIP_CATEGORIES)
_SPACE_LOOKALIKE_RE = re.compile(r"[\u2800\u3164]")

# Any non-word, non-space char between word characters → space
_PUNCT_SEPARATOR_RE = re.compile(r"(?<=\w)[^\w\s](?=\w)")


class RegexDetector(BaseDetector):
    def __init__(self, patterns=None):
        self.patterns = ALL_PATTERNS if patterns is None else patterns
        self._compiled = [
            (re.compile(p, re.IGNORECASE | re.UNICODE), cat, weight)
            for p, cat, weight in self.patterns
        ]

    @property
    def name(self) -> str:
        return "injection"

    @staticmethod
    def _strip_invisible(text: str) -> str:
        """Strip all characters in Cf, Mn, Me Unicode categories."""
        return "".join(
            ch for ch in text if unicodedata.category(ch) not in _STRIP_CATEGORIES
        )

    @staticmethod
    def _normalize_text(text: str) -> str:
        # 1. Replace space lookalikes BEFORE NFKC (which may convert them)
        text = _SPACE_LOOKALIKE_RE.sub(" ", text)
        # 2. NFKC normalization (fullwidth → ASCII, ligatures, compatibility chars)
        text = unicodedata.normalize("NFKC", text)
        # 3. Confusable homoglyph mapping (Cyrillic/Greek/Armenian → Latin)
        text = text.translate(_CONFUSABLE_MAP)
        # 4. NFD decompose, strip invisible chars by category, recompose
        text = unicodedata.normalize("NFD", text)
        text = RegexDetector._strip_invisible(text)
        text = unicodedata.normalize("NFC", text)
        # 5. Normalize punctuation separators to spaces
        text = _PUNCT_SEPARATOR_RE.sub(" ", text)
        return text

    def detect(self, text: str) -> InjectionResult:
        normalized = self._normalize_text(text)

        if self._is_allowlisted(normalized):
            return InjectionResult(
                detection_method=self.name,
            )

        matches = []
        max_score = 0.0
        seen = set()

        # Match against normalized text (catches attacks with invisible chars/punctuation)
        for compiled, category, weight in self._compiled:
            found = compiled.search(normalized)
            if found:
                key = (category, weight)
                seen.add(key)
                matches.append(
                    {
                        "category": category,
                        "matched_text": found.group(0),
                        "weight": weight,
                    }
                )
                max_score = max(max_score, weight)

        # Also match against original text (catches encoding patterns like invisible_chars)
        if text != normalized:
            for compiled, category, weight in self._compiled:
                found = compiled.search(text)
                if found:
                    key = (category, weight)
                    if key not in seen:
                        seen.add(key)
                        matches.append(
                            {
                                "category": category,
                                "matched_text": found.group(0),
                                "weight": weight,
                            }
                        )
                        max_score = max(max_score, weight)

        # Graduated boost when multiple distinct categories match.
        # More categories hitting simultaneously suggests a deliberate attack,
        # so the score is bumped to push borderline results into higher severity.
        categories = {m["category"] for m in matches}
        n_cats = len(categories)
        if n_cats >= 4:
            max_score = min(1.0, max_score + 0.20)  # 4+ categories → strong boost
        elif n_cats >= 3:
            max_score = min(1.0, max_score + 0.15)  # 3 categories → moderate boost
        elif n_cats >= 2:
            max_score = min(1.0, max_score + 0.10)  # 2 categories → mild boost

        severity = SeverityLevel.from_score(max_score)
        action = DetectionAction.from_injection_severity(severity)

        return InjectionResult(
            score=max_score,
            severity=severity,
            action=action,
            matched_patterns=matches,
            detection_method=self.name,
        )

    def _is_allowlisted(self, text: str) -> bool:
        try:
            allowlist_raw = getattr(config, "LLM_INJECTION_ALLOWLIST", "")
        except Exception:
            logger.exception("Failed to read LLM_INJECTION_ALLOWLIST config")
            return False
        allowlist = [
            phrase.strip() for phrase in allowlist_raw.split(",") if phrase.strip()
        ]
        if not allowlist:
            return False
        text_lower = text.strip().lower()
        text_len = len(text_lower)
        if text_len == 0:
            return False
        for term in allowlist:
            term_lower = term.lower()
            if re.search(r"\b" + re.escape(term_lower) + r"\b", text_lower):
                if len(term_lower) / text_len > 0.8:
                    logger.debug(
                        "Allowlist bypass: term=%r covered %.0f%% of input (len=%d)",
                        term,
                        (len(term_lower) / text_len) * 100,
                        text_len,
                    )
                    return True
        return False
