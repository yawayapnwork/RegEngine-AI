"""Requirement 2 -- Cross-Lingual Entity Alignment: maps a regional-
language entity phrase directly onto this platform's existing SEBI
entity taxonomy (`app.agents.tools.SEBI_ENTITY_TAXONOMY` -- the SAME
canonical names the Extraction/Logic-Auditor agents and
`app.agents.tools.lookup_entity` already resolve English phrases
against), so a Hindi/Marathi/Gujarati clause and its English
counterpart both normalize onto identical `TargetEntity.normalized_entity`
values downstream, with no separate regional-language taxonomy to keep
in sync.

Two-tier resolution, cheapest first:
  1. Direct glossary match against `REGIONAL_ENTITY_ALIASES` below -- no
     translation call needed, so the common case (a standard SEBI
     entity term appearing verbatim) resolves instantly and
     deterministically.
  2. Translated fallback: if no glossary entry matches, translate the
     phrase and resolve it via the EXISTING `app.agents.tools.lookup_entity`
     -- reused unmodified, so English-side entity resolution logic
     never has to be duplicated or kept in sync across two code paths.

`REGIONAL_ENTITY_ALIASES` is a maintained, hand-curated glossary (same
spirit as `SEBI_ENTITY_TAXONOMY`'s own "extend as needed" scope) -- not
a claim of exhaustive linguistic coverage. A phrase this glossary
doesn't recognize correctly falls through to tier 2 rather than
silently mismatching.
"""
from __future__ import annotations

import unicodedata
from typing import Callable

from pydantic import BaseModel

from app.agents.tools import SEBI_ENTITY_TAXONOMY, EntityLookupInput, lookup_entity
from app.localization.languages import RegionalLanguage

# canonical SEBI entity name -> {language -> [regional aliases]}. Every
# canonical key here MUST also be a key in SEBI_ENTITY_TAXONOMY -- see
# the assertion at module load time below, which fails fast (at import,
# not at first lookup) if this glossary and the English taxonomy ever
# drift apart.
REGIONAL_ENTITY_ALIASES: dict[str, dict[str, list[str]]] = {
    "Stockbroker": {
        "hi": ["स्टॉक ब्रोकर", "शेयर दलाल", "शेयर ब्रोकर", "ब्रोकर"],
        "mr": ["स्टॉक ब्रोकर", "समभाग दलाल", "ब्रोकर"],
        "gu": ["સ્ટોક બ્રોકર", "શેર દલાલ", "બ્રોકર"],
    },
    "Depository Participant": {
        "hi": ["डिपॉजिटरी प्रतिभागी"],
        "mr": ["डिपॉझिटरी सहभागी"],
        "gu": ["ડિપોઝિટરી સહભાગી"],
    },
    "Asset Management Company": {
        "hi": ["परिसंपत्ति प्रबंधन कंपनी", "एसेट मैनेजमेंट कंपनी"],
        "mr": ["मालमत्ता व्यवस्थापन कंपनी"],
        "gu": ["એસેટ મેનેજમેન્ટ કંપની"],
    },
    "Mutual Fund Trustee": {
        "hi": ["म्यूचुअल फंड ट्रस्टी", "न्यासी"],
        "mr": ["म्युच्युअल फंड विश्वस्त"],
        "gu": ["મ્યુચ્યુઅલ ફંડ ટ્રસ્ટી"],
    },
    "Custodian": {
        "hi": ["अभिरक्षक", "कस्टोडियन"],
        "mr": ["अभिरक्षक", "कस्टोडियन"],
        "gu": ["કસ્ટોડિયન"],
    },
    "Clearing Corporation": {
        "hi": ["समाशोधन निगम", "क्लियरिंग कॉर्पोरेशन"],
        "mr": ["क्लिअरिंग कॉर्पोरेशन"],
        "gu": ["ક્લિયરિંગ કોર્પોરેશન"],
    },
    "Stock Exchange": {
        "hi": ["स्टॉक एक्सचेंज", "शेयर बाजार"],
        "mr": ["शेअर बाजार", "स्टॉक एक्स्चेंज"],
        "gu": ["શેર બજાર", "સ્ટોક એક્સચેન્જ"],
    },
    "Merchant Banker": {
        "hi": ["मर्चेंट बैंकर"],
        "mr": ["मर्चंट बँकर"],
        "gu": ["મર્ચન્ટ બેન્કર"],
    },
    "Credit Rating Agency": {
        "hi": ["क्रेडिट रेटिंग एजेंसी", "साख निर्धारण एजेंसी"],
        "mr": ["पत मानांकन संस्था", "क्रेडिट रेटिंग एजन्सी"],
        "gu": ["ક્રેડિટ રેટિંગ એજન્સી"],
    },
    "Investment Adviser": {
        "hi": ["निवेश सलाहकार"],
        "mr": ["गुंतवणूक सल्लागार"],
        "gu": ["રોકાણ સલાહકાર"],
    },
    "Portfolio Manager": {
        "hi": ["पोर्टफोलियो प्रबंधक"],
        "mr": ["पोर्टफोलिओ व्यवस्थापक"],
        "gu": ["પોર્ટફોલિયો મેનેજર"],
    },
    "Registrar and Transfer Agent": {
        "hi": ["रजिस्ट्रार एवं अंतरण अभिकर्ता", "रजिस्ट्रार एंड ट्रांसफर एजेंट"],
        "mr": ["रजिस्ट्रार आणि ट्रान्सफर एजंट"],
        "gu": ["રજિસ્ટ્રાર એન્ડ ટ્રાન્સફર એજન્ટ"],
    },
    "KYC Registration Agency": {
        "hi": ["केवाईसी पंजीकरण एजेंसी"],
        "mr": ["केवायसी नोंदणी संस्था"],
        "gu": ["કેવાયસી નોંધણી એજન્સી"],
    },
    "Research Analyst": {
        "hi": ["अनुसंधान विश्लेषक", "शोध विश्लेषक"],
        "mr": ["संशोधन विश्लेषक"],
        "gu": ["સંશોધન વિશ્લેષક"],
    },
    "Alternative Investment Fund": {
        "hi": ["वैकल्पिक निवेश कोष", "वैकल्पिक निवेश फंड"],
        "mr": ["पर्यायी गुंतवणूक निधी"],
        "gu": ["વૈકલ્પિક રોકાણ ફંડ"],
    },
    "Foreign Portfolio Investor": {
        "hi": ["विदेशी पोर्टफोलियो निवेशक"],
        "mr": ["परदेशी पोर्टफोलिओ गुंतवणूकदार"],
        "gu": ["વિદેશી પોર્ટફોલિયો રોકાણકાર"],
    },
}

_unknown_keys = set(REGIONAL_ENTITY_ALIASES) - set(SEBI_ENTITY_TAXONOMY)
assert not _unknown_keys, f"REGIONAL_ENTITY_ALIASES has canonical name(s) not present in SEBI_ENTITY_TAXONOMY: {_unknown_keys}"


class EntityAlignmentResult(BaseModel):
    input_phrase: str
    language: RegionalLanguage
    normalized_entity: str | None
    confidence: float
    resolved: bool
    method: str = "unresolved"  # "direct_glossary" | "translated_fallback" | "unresolved"
    translated_phrase: str | None = None


def _canon(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def _direct_glossary_lookup(phrase: str, language: RegionalLanguage) -> tuple[str | None, float]:
    phrase_canon = _canon(phrase)
    best_entity: str | None = None
    best_score = 0.0

    for canonical, by_language in REGIONAL_ENTITY_ALIASES.items():
        for alias in by_language.get(language.value, []):
            alias_canon = _canon(alias)
            if alias_canon == phrase_canon:
                return canonical, 1.0
            if alias_canon in phrase_canon or phrase_canon in alias_canon:
                score = len(alias_canon) / max(len(phrase_canon), 1)
                if score > best_score:
                    best_score, best_entity = score, canonical

    return (best_entity, best_score) if best_score >= 0.5 else (None, 0.0)


def align_regional_entity(
    phrase: str,
    language: RegionalLanguage,
    *,
    translate_fn: Callable[[str], str] | None = None,
) -> EntityAlignmentResult:
    """`translate_fn`, when given, is called ONLY if the direct glossary
    misses -- typically `app.localization.translation`'s configured
    backend's `.translate_text(phrase, language)`, injected rather than
    imported directly so this function has no hard dependency on
    IndicTrans2/NLLB being installed for the (common) case where the
    glossary already resolves the phrase."""
    canonical, confidence = _direct_glossary_lookup(phrase, language)
    if canonical is not None:
        return EntityAlignmentResult(
            input_phrase=phrase, language=language, normalized_entity=canonical,
            confidence=confidence, resolved=True, method="direct_glossary",
        )

    if translate_fn is None:
        return EntityAlignmentResult(input_phrase=phrase, language=language, normalized_entity=None, confidence=0.0, resolved=False)

    translated_phrase = translate_fn(phrase)
    fallback = lookup_entity(EntityLookupInput(entity_phrase=translated_phrase))
    return EntityAlignmentResult(
        input_phrase=phrase,
        language=language,
        normalized_entity=fallback.normalized_entity,
        confidence=fallback.confidence,
        resolved=fallback.resolved,
        method="translated_fallback" if fallback.resolved else "unresolved",
        translated_phrase=translated_phrase,
    )
