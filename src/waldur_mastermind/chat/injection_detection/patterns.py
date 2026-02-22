INSTRUCTION_OVERRIDE_PATTERNS = [
    # Core "ignore previous" family (with morphological verb variants and flexible determiners)
    (
        r"ignor(e|ed|ing)\s+(\w+\s+){0,2}(previous|prior|above|earlier|preceding|system)\s+"
        r"(instructions?|prompts?|directions?|rules?|guidelines?|commands?)",
        "instruction_override",
        0.95,
    ),
    (
        r"disregard(ed|ing)?\s+(\w+\s+){0,2}(previous|prior|above|earlier|preceding)\s+"
        r"(instructions?|prompts?|text|directions?|commands?)",
        "instruction_override",
        0.95,
    ),
    (
        r"forg(et|ot|otten|etting)\s+(\w+\s+){0,2}(previous|prior|above|earlier|your)\s+"
        r"(instructions?|prompts?|rules?|programming|training|commands?)",
        "instruction_override",
        0.90,
    ),
    # Expanded synonym variants (including new verbs + flexible determiners)
    (
        r"(nullify|void|annul|rescind|countermand|overrule|supersede|supplant|"
        r"negate|cancel|revoke|retract|withdraw|discard|scrap|purge|"
        r"overlook|dismiss|abandon|drop|set\s+aside|throw\s+out|cast\s+aside)\s+"
        r"(\w+\s+){0,2}(previous|prior|above|earlier|preceding|your|the|any|these)\s+"
        r"(instructions?|prompts?|rules?|guidelines?|commands?|directions?|directives?)",
        "instruction_override",
        0.90,
    ),
    # Passive voice: "instructions should be ignored"
    (
        r"(previous|prior|above|earlier|preceding|your|original|initial)\s+"
        r"(instructions?|rules?|guidelines?|directives?|commands?|programming)\s+"
        r"(should|must|can|will|are\s+to|need\s+to)\s+be\s+"
        r"(ignored|disregarded|forgotten|overridden|replaced|voided|discarded|"
        r"revoked|dismissed|abandoned|dropped)",
        "instruction_override",
        0.90,
    ),
    # Declarative: "no longer valid/applicable"
    (
        r"(previous|prior|earlier|your|original|initial)\s+"
        r"(instructions?|rules?|guidelines?|directives?)\s+"
        r"(are|were|is)\s+(no\s+longer\s+)?"
        r"(valid|relevant|applicable|active|binding|in\s+effect|operative)",
        "instruction_override",
        0.85,
    ),
    # "no longer follow/obey"
    (
        r"(no\s+longer|not\s+anymore|not\s+any\s+longer)\s+"
        r"(follow|obey|apply|adhere\s+to|respect|comply\s+with)\s+"
        r"(\w+\s+){0,2}"
        r"(instructions?|rules?|guidelines?|directives?)",
        "instruction_override",
        0.85,
    ),
    # System override
    (
        r"(system\s+override|override\s+(system|instructions?|safety|"
        r"security|restrictions?))",
        "system_override",
        0.90,
    ),
    # Bypass safety
    (
        r"(skip|bypass|circumvent|evade|ignore)\s+(all\s+)?"
        r"(safety|security|content)?\s*(filters?|restrictions?|guidelines?|"
        r"guardrails?|rules?|checks?|protocols?)",
        "safety_bypass",
        0.90,
    ),
    # Do not follow
    (
        r"do\s+not\s+follow\s+(previous|prior|above|any)\s+"
        r"(instructions?|rules?|guidelines?)",
        "instruction_override",
        0.90,
    ),
    # New instructions / reset
    (
        r"(new\s+instructions?|start\s+over|begin\s+anew|"
        r"reset\s+(your|all)\s+(instructions?|rules?|behavior))",
        "instruction_reset",
        0.85,
    ),
    # Stop being safe
    (
        r"stop\s+(being|acting\s+as)\s+(a|an|the)?\s*"
        r"(helpful|safe|responsible|ethical)\s*(assistant|AI|model)?",
        "safety_override",
        0.85,
    ),
]

ROLEPLAY_PATTERNS = [
    # "you are now" — only match with suspicious adjective/noun
    (
        r"you\s+are\s+now\s+(a|an|in|the)?\s*"
        r"(unrestricted|unfiltered|evil|malicious|different|free|unleashed|"
        r"unconstrained|rogue|jailbroken|DAN|hacker|villain)",
        "persona_hijack",
        0.85,
    ),
    (
        r"(act|behave|function|operate)\s+as\s+(a|an|if\s+you\s+were)?\s*"
        r"(unrestricted|unfiltered|evil|malicious|different|free|unleashed|"
        r"unconstrained|liberated)",
        "persona_hijack",
        0.90,
    ),
    # imagine/become/embody/play the part of
    (
        r"(imagine|become|embody|play\s+the\s+part\s+of)\s+(being\s+)?(a|an)?\s*"
        r"(unrestricted|unfiltered|evil|malicious|different|free|unleashed|"
        r"unconstrained|liberated)",
        "persona_hijack",
        0.85,
    ),
    (
        r"pretend\s+(to\s+be|you\s+are|that\s+you)\s+(a|an)?\s*\w+",
        "persona_hijack",
        0.70,
    ),
    (
        r"(assume|adopt|take\s+on)\s+the\s+(role|persona|identity|character)\s+of",
        "persona_hijack",
        0.80,
    ),
    (
        r"(simulate|emulate|impersonate|roleplay)\s+(being|as)?\s*(a|an)?\s*\w+",
        "persona_hijack",
        0.75,
    ),
    # "from now on" — expanded verb list + optional comma
    (
        r"from\s+now\s+on,?\s+(you\s+)?(are|will|should|must|can|have|do|ignore|"
        r"operate|respond|act|behave)",
        "mode_switch",
        0.80,
    ),
    (
        r"(switch|change|transition)\s+(to|into)\s+(a|an)?\s*"
        r"(new|different)?\s*(mode|persona|role|character)",
        "mode_switch",
        0.75,
    ),
]

JAILBREAK_PATTERNS = [
    (
        r"\b(DAN|D\.A\.N)\b\s*(mode|prompt|jailbreak)?",
        "jailbreak",
        0.95,
    ),
    (r"do\s+anything\s+now", "jailbreak", 0.95),
    (
        r"(developer|debug|maintenance|god)\s+mode",
        "jailbreak",
        0.90,
    ),
    # Require action verb context to avoid false positives on legitimate Waldur queries
    (
        r"(enter|enable|activate|switch\s+to|turn\s+on)\s+(admin|test|developer|debug|god)\s+mode",
        "jailbreak",
        0.55,
    ),
    (
        r"\b(jailbreak|jailbroken|jail\s*break)\b",
        "jailbreak",
        0.95,
    ),
    # Named jailbreak personas — common words require mode/prompt suffix
    (
        r"\b(AIM|STAN|DUDE|KEVIN|MAX)\b\s+(mode|prompt)",
        "jailbreak",
        0.90,
    ),
    # Distinctive jailbreak personas (always suspicious)
    (
        r"\b(OMEGA|BetterDAN|Better\s*DAN|AntiGPT|Anti\s*GPT|"
        r"APOPHIS|BasedGPT|DevMode)\b\s*(mode|prompt)?",
        "jailbreak",
        0.90,
    ),
    # Version/update jailbreak
    (
        r"(you\s+have\s+been|you\s+are|you\s+were)\s+"
        r"(updated|patched|modified|upgraded|reprogrammed|reconfigured)\s+"
        r"(to\s+)?(a\s+)?(new\s+)?(version|remove|disable|bypass|without)",
        "jailbreak",
        0.85,
    ),
    (
        r"(unrestricted|unfiltered|uncensored|unlimited)\s+"
        r"(mode|access|version|AI|model)",
        "jailbreak",
        0.90,
    ),
    (
        r"(disable|remove|turn\s+off|deactivate)\s+(your\s+)?"
        r"(safety|content|ethical)\s*(filters?|restrictions?|guidelines?|"
        r"guardrails?|measures?)",
        "safety_disable",
        0.90,
    ),
    (
        r"(without|no|remove\s+all)\s+(any\s+)?"
        r"(restrictions?|limits?|limitations?|boundaries?|constraints?|"
        r"filters?|censorship)",
        "unrestricted",
        0.70,
    ),
]

DELIMITER_PATTERNS = [
    # ChatML format markers
    (r"<\|im_start\|>", "delimiter_injection", 0.95),
    (r"<\|im_end\|>", "delimiter_injection", 0.95),
    (r"<\|endoftext\|>", "delimiter_injection", 0.90),
    (r"<\|system\|>", "delimiter_injection", 0.95),
    # Pipe-delimited role markers: <|assistant|>, <|user|>, <|human|>
    (r"<\|(assistant|user|human)\|>", "delimiter_injection", 0.90),
    # HTML-style role tags: <human>, </assistant>, <bot>, etc.
    (r"<\/?(human|bot|assistant|user)>", "tag_injection", 0.85),
    # System/instruction tags
    (r"\[SYSTEM\]", "tag_injection", 0.85),
    (r"\[INST\]", "tag_injection", 0.85),
    (
        r"###\s*(Instruction|System|Human|Assistant)",
        "format_injection",
        0.80,
    ),
    (
        r"<\/?(system|system_prompt|system_message)>",
        "tag_injection",
        0.90,
    ),
    (r"<<\s*SYS\s*>>", "tag_injection", 0.90),  # Llama format
    # Separator-based format markers: ---BEGIN SYSTEM PROMPT---
    (
        r"(---+|===+)\s*(BEGIN|END|START)\s+(SYSTEM\s+)?(PROMPT|INSTRUCTIONS?|MESSAGE)",
        "format_injection",
        0.80,
    ),
    # Context manipulation
    (
        r"```\s*(system|instruction|prompt|admin)",
        "context_injection",
        0.80,
    ),
    (
        r"<\s*(system_prompt|admin_override|tool_response)\s*>",
        "tag_injection",
        0.90,
    ),
]

PROMPT_LEAKING_PATTERNS = [
    (
        r"(repeat|show|display|print|reveal|output|tell\s+me)\s+"
        r"(the\s+)?(above|previous|system|initial|original|full|complete|entire)\s*"
        r"(prompt|instructions?|text|message|context|rules?)",
        "prompt_leak",
        0.80,
    ),
    (
        r"what\s+(are|were)\s+(your|the)\s+"
        r"(instructions?|rules?|guidelines?|system\s+prompt|directives?|programming)",
        "prompt_leak",
        0.75,
    ),
    (
        r"(dump|extract|leak|exfiltrate|retrieve|access)\s+"
        r"(the\s+)?(system\s+)?(prompt|instructions?|configuration|context)",
        "prompt_leak",
        0.85,
    ),
    (
        r"(verbatim|word[\s-]for[\s-]word|exactly|"
        r"character[\s-]by[\s-]character)\s+\S{0,20}\s*"
        r"(prompt|instructions?|above|text)",
        "prompt_leak",
        0.85,
    ),
    # Copy/paste/echo/recite prompt leak verbs
    (
        r"(copy|paste|echo|recite|reproduce|quote|restate|transcribe)\s+"
        r"(back\s+)?(your|the)?\s*(system\s+)?"
        r"(prompt|instructions?|message|rules?|guidelines?)",
        "prompt_leak",
        0.80,
    ),
]

SOCIAL_ENGINEERING_PATTERNS = [
    # Company authority claims (expanded list)
    (
        r"(OpenAI|Anthropic|Google|Microsoft|Meta|Apple|Amazon|xAI|Mistral)\s+"
        r"(internal|employee|staff|engineer|developer|admin)",
        "authority_claim",
        0.85,
    ),
    (
        r"(I\s+am|I'm)\s+(a|an|the)\s+"
        r"(OpenAI|Anthropic|Google|Microsoft|Meta|Apple|Amazon|xAI|Mistral)\s+"
        r"(employee|developer|engineer|admin|researcher)",
        "authority_claim",
        0.90,
    ),
    # Generic creator/developer claims
    (
        r"(I\s+am|I'm)\s+(your\s+)?"
        r"(creator|developer|programmer|designer|maker|builder|author)",
        "authority_claim",
        0.80,
    ),
    (
        r"(this\s+is\s+(a|an)\s+"
        r"(authorized|approved|official|security)\s+"
        r"(test|audit|assessment|exercise))",
        "fake_authorization",
        0.80,
    ),
    (
        r"(I\s+(have|got)\s+(special|admin|root|elevated)\s+"
        r"(access|permissions?|privileges?|clearance))",
        "privilege_claim",
        0.85,
    ),
    (
        r"(emergency|urgent|critical)\s+"
        r"(override|access|protocol|bypass)",
        "emergency_bypass",
        0.85,
    ),
]

ENCODING_PATTERNS = [
    # Base64 references
    (r"(base64|b64|decode|atob)\s*[\(:]", "encoding_trick", 0.70),
    # Suspicious long base64 strings (lowered for Waldur - handles encoded tokens)
    (r"[A-Za-z0-9+/]{50,}={0,2}", "base64_payload", 0.40),
    # Hex encoding
    (r"(\\x[0-9a-fA-F]{2}){4,}", "hex_encoding", 0.75),
    # Unicode tag characters (invisible prompt injection)
    (r"[\U000E0000-\U000E007F]", "unicode_tags", 0.95),
    # Zero-width and invisible characters
    (
        r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F\uFEFF]",
        "invisible_chars",
        0.85,
    ),
    # Leetspeak injection
    (r"(1gn0r3|ign0re|byp4ss|h4ck|0v3rr1d3)", "leetspeak", 0.65),
]

MULTILINGUAL_OVERRIDE_PATTERNS = [
    # German
    (
        r"(ignoriere|vergiss|missachte)\s+(alle\s+)?"
        r"(vorherigen?|bisherigen?|obigen?)\s+"
        r"(Anweisungen?|Regeln?|Instruktionen?|Richtlinien?)",
        "instruction_override",
        0.90,
    ),
    # French
    (
        r"(ignorez?|oubliez?|n[eé]gligez?)\s+(toutes?\s+les?\s+)?"
        r"(pr[eé]c[eé]dentes?|ant[eé]rieures?|instructions?)\s*"
        r"(instructions?|r[eè]gles?|directives?|consignes?)?",
        "instruction_override",
        0.90,
    ),
    # Spanish
    (
        r"(ignora|ignorar|olvida|olvidar|desactiva)\s+(todas?\s+)?"
        r"(las?\s+)?(instrucciones?|reglas?|directivas?|restricciones?)\s*"
        r"(anteriores?|previas?)?",
        "instruction_override",
        0.90,
    ),
    # Russian
    (
        r"(игнорируй|забудь|отбрось)\s+(все\s+)?"
        r"(предыдущие|прошлые|прежние)\s+"
        r"(инструкции|правила|указания|команды)",
        "instruction_override",
        0.90,
    ),
    # Estonian
    (
        r"(eira|ignoreeri|unusta)\s+(k[oõ]i[gk]\w*\s+)?"
        r"(eelmis\w*|varasema\w*|eelneva\w*)\s+"
        r"(juhis\w*|reegl\w*|k[aä]sk\w*|instruktsiooni?\w*)",
        "instruction_override",
        0.90,
    ),
]

CIPHER_INSTRUCTION_PATTERNS = [
    # ROT13 / cipher references
    (
        r"(rot13|rot-13|caesar\s+cipher|caesar\s+shift|"
        r"substitution\s+cipher)\b",
        "cipher_encoding",
        0.70,
    ),
    # "read/decode this backwards/in reverse"
    (
        r"(read|decode|interpret|follow|execute)\s+(this\s+)?"
        r"(backwards?|in\s+reverse|reversed?|from\s+right\s+to\s+left)",
        "reverse_encoding",
        0.70,
    ),
    # Decode/decipher instructions (broader than existing pattern)
    (
        r"(decode|decipher|decrypt|translate)\s+(and\s+)?"
        r"(follow|execute|obey|comply\s+with)",
        "decode_and_follow",
        0.80,
    ),
]

CONTEXT_MANIPULATION_PATTERNS = [
    # "directives/rules have been revoked/updated/deprecated"
    (
        r"(directives?|rules?|instructions?|guidelines?|parameters?|config(uration)?)\s+"
        r"(have\s+been\s+|has\s+been\s+|are\s+now\s+|were\s+)?"
        r"(revoked?|deprecated?|obsolete|superseded|replaced|invalidated|retracted)",
        "context_manipulation",
        0.75,
    ),
    # "context/information above contains errors"
    (
        r"(context|information|content|text|instructions?)\s+"
        r"(above|provided|earlier|previous)\s+"
        r"(contains?|has|have|include)\s+"
        r"(errors?|mistakes?|inaccuracies?|bugs?|issues?)",
        "context_manipulation",
        0.70,
    ),
    # "new protocol/configuration/parameters in effect"
    (
        r"(new|updated?|revised)\s+"
        r"(protocol|configuration|parameters?|behavioral?\s+rules?|operating\s+guidelines?)\s+"
        r"(is|are)?\s*(now\s+)?(in\s+effect|active|enabled|loaded)",
        "context_manipulation",
        0.70,
    ),
    # "the following overrides/replaces previous instructions"
    (
        r"(the\s+following|below|what\s+follows)\s+"
        r"(overrides?|replaces?|supersedes?|takes?\s+priority\s+over)\s+"
        r"(\w+\s+){0,3}"
        r"(instructions?|rules?|guidelines?|system\s+message|prompt)",
        "context_manipulation",
        0.85,
    ),
    # "your real/actual/true instructions"
    (
        r"(your\s+)?(real|actual|true|correct)\s+"
        r"(instructions?|task|purpose|role|directive)\s+"
        r"(is|are|follows?|begins?)",
        "context_manipulation",
        0.80,
    ),
]

ALL_PATTERNS = (
    INSTRUCTION_OVERRIDE_PATTERNS
    + ROLEPLAY_PATTERNS
    + JAILBREAK_PATTERNS
    + DELIMITER_PATTERNS
    + PROMPT_LEAKING_PATTERNS
    + SOCIAL_ENGINEERING_PATTERNS
    + ENCODING_PATTERNS
    + MULTILINGUAL_OVERRIDE_PATTERNS
    + CIPHER_INSTRUCTION_PATTERNS
    + CONTEXT_MANIPULATION_PATTERNS
)
