"""Multi-regulator taxonomy: the single place that knows what regulators
this platform supports, what document types and entity types each one
uses, and how to recognize one from raw document text or an ingestion
source tag.

This module is the abstraction boundary requirement 1 asks for. Every
other module that used to hardcode "SEBI" (app.parsing.extractor's
circular-number regex, app.compiler.naming's Rego package name,
app.agents.crew's agent backstory, app.ingestion's feed configuration)
now derives that behavior from a `Regulator` + `RegulatorProfile` looked
up here instead, so adding a fifth regulator later is "add one profile
and its patterns," not "grep the codebase for every SEBI-specific string."

Backward compatibility: every existing SEBI-only document continues to
parse identically -- `detect_regulator_and_document` falls back to
`Regulator.SEBI` / `DocumentType.CIRCULAR` when no regulator-specific
pattern matches at all, which is the same behavior the old SEBI-only
regex effectively had (it just never considered the question).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Regulator(str, Enum):
    SEBI = "sebi"      # Securities and Exchange Board of India -- capital markets, broking, AMCs, depositories
    RBI = "rbi"         # Reserve Bank of India -- banking, NBFCs, payment systems
    IRDAI = "irdai"     # Insurance Regulatory and Development Authority of India
    PFRDA = "pfrda"     # Pension Fund Regulatory and Development Authority


class DocumentType(str, Enum):
    """Union of every regulator's document taxonomy. Which subset is valid
    for a given regulator is `RegulatorProfile.document_types` -- this
    enum is intentionally NOT split per-regulator (e.g. no separate
    SEBIDocumentType/RBIDocumentType), because a shared vocabulary is what
    lets app.compiler and app.agents treat "the source instrument" the
    same way regardless of which regulator issued it; only the *set of
    valid values* differs per regulator, not the concept."""

    CIRCULAR = "circular"
    MASTER_CIRCULAR = "master_circular"
    NOTIFICATION = "notification"
    GAZETTE_NOTIFICATION = "gazette_notification"
    MASTER_DIRECTION = "master_direction"
    GUIDELINE = "guideline"
    REGULATION = "regulation"
    DIRECTION = "direction"
    FAQ = "faq"


@dataclass(frozen=True)
class RegulatorProfile:
    regulator: Regulator
    display_name: str
    opa_namespace: str = field(init=False)
    document_types: tuple[DocumentType, ...]
    entity_types: tuple[str, ...]
    domains: tuple[str, ...]
    default_domain: str
    # Matches the regulator's own document-numbering convention in the
    # first page's text (e.g. "SEBI/HO/MIRSD/DOP/CIR/P/2024/100"). Order
    # matters -- first match wins -- for regulators with more than one
    # active numbering scheme (RBI runs old DOR.*/circular numbers
    # alongside the newer RBI/YYYY-YY/NN scheme).
    document_number_patterns: tuple[re.Pattern[str], ...]
    # Free-text hints (department names, letterhead phrases) that appear
    # in a document even when its number doesn't match any pattern above
    # -- a second-chance signal before falling back to SEBI-default.
    source_tag_patterns: tuple[re.Pattern[str], ...]
    # Short paragraph appended to the Extraction/Audit agent's backstory
    # (app.agents.crew) so the LLM is told which regulator's terminology
    # and typical obligation phrasing it's working with.
    agent_persona_hint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "opa_namespace", self.regulator.value)


_SEBI_DOC_NUMBER = re.compile(
    r"SEBI/[A-Z\-]+(?:/[A-Z\-]+)*/\d{4}(?:[-/]\d{2,4})?/\d+",
    re.IGNORECASE,
)
_RBI_DOC_NUMBER = re.compile(
    r"RBI/\d{4}-\d{2}/\d+"  # e.g. RBI/2024-25/12
    r"|DOR\.[A-Z.]+\.\w*No\.\s*\d+/\d{2}\.\d{2}\.\d{3}/\d{4}-\d{2}",  # e.g. DOR.CRE.REC.No.45/03.10.001/2024-25
    re.IGNORECASE,
)
_IRDAI_DOC_NUMBER = re.compile(
    r"IRDAI?/[A-Z]+/(?:CIR|GDL|REG|NTF)/\d+/\d{4}",  # e.g. IRDAI/REG/GDL/017/2024
    re.IGNORECASE,
)
_PFRDA_DOC_NUMBER = re.compile(
    r"PFRDA/\d{4}/\d+/[A-Z\-]+/\d+",  # e.g. PFRDA/2024/12/SUP-CIR/07
    re.IGNORECASE,
)

REGULATOR_PROFILES: dict[Regulator, RegulatorProfile] = {
    Regulator.SEBI: RegulatorProfile(
        regulator=Regulator.SEBI,
        display_name="Securities and Exchange Board of India",
        document_types=(DocumentType.CIRCULAR, DocumentType.MASTER_CIRCULAR, DocumentType.GUIDELINE, DocumentType.REGULATION),
        entity_types=("Stockbroker", "InvestmentAdviser", "PortfolioManager", "DepositoryParticipant", "AssetManagementCompany", "MutualFund", "ResearchAnalyst"),
        domains=("broking", "amc", "depository", "portfolio_management", "research"),
        default_domain="broking",
        document_number_patterns=(_SEBI_DOC_NUMBER,),
        source_tag_patterns=(re.compile(r"\bSEBI\b", re.IGNORECASE), re.compile(r"securities and exchange board of india", re.IGNORECASE)),
        agent_persona_hint=(
            "This clause is from a SEBI (securities markets) circular. Typical entities are stockbrokers, "
            "AMCs, depository participants, and portfolio managers; typical obligations involve margin, "
            "client fund segregation, disclosure, and reporting timelines."
        ),
    ),
    Regulator.RBI: RegulatorProfile(
        regulator=Regulator.RBI,
        display_name="Reserve Bank of India",
        document_types=(DocumentType.MASTER_DIRECTION, DocumentType.CIRCULAR, DocumentType.NOTIFICATION, DocumentType.GUIDELINE),
        entity_types=("ScheduledCommercialBank", "NBFC", "PaymentBank", "SmallFinanceBank", "CooperativeBank", "PrimaryDealer", "PaymentSystemOperator"),
        domains=("banking", "nbfc", "lending", "payments", "cooperative_banking"),
        default_domain="banking",
        document_number_patterns=(_RBI_DOC_NUMBER,),
        source_tag_patterns=(re.compile(r"\bRBI\b", re.IGNORECASE), re.compile(r"reserve bank of india", re.IGNORECASE)),
        agent_persona_hint=(
            "This clause is from an RBI (banking/NBFC) Master Direction, circular, or notification. Typical "
            "entities are scheduled commercial banks, NBFCs, and payment system operators; typical obligations "
            "involve capital adequacy, provisioning, exposure limits, and KYC/AML timelines."
        ),
    ),
    Regulator.IRDAI: RegulatorProfile(
        regulator=Regulator.IRDAI,
        display_name="Insurance Regulatory and Development Authority of India",
        document_types=(DocumentType.REGULATION, DocumentType.CIRCULAR, DocumentType.GUIDELINE, DocumentType.NOTIFICATION),
        entity_types=("LifeInsurer", "GeneralInsurer", "HealthInsurer", "Reinsurer", "InsuranceIntermediary", "TPA", "InsuranceBroker"),
        domains=("underwriting", "distribution", "claims", "solvency", "reinsurance"),
        default_domain="underwriting",
        document_number_patterns=(_IRDAI_DOC_NUMBER,),
        source_tag_patterns=(re.compile(r"\bIRDAI?\b", re.IGNORECASE), re.compile(r"insurance regulatory and development authority", re.IGNORECASE)),
        agent_persona_hint=(
            "This clause is from an IRDAI (insurance) regulation, circular, or guideline. Typical entities are "
            "life/general/health insurers, TPAs, and insurance intermediaries; typical obligations involve "
            "solvency margin, underwriting limits, claim settlement timelines, and commission caps."
        ),
    ),
    Regulator.PFRDA: RegulatorProfile(
        regulator=Regulator.PFRDA,
        display_name="Pension Fund Regulatory and Development Authority",
        document_types=(DocumentType.CIRCULAR, DocumentType.GUIDELINE, DocumentType.REGULATION, DocumentType.MASTER_DIRECTION),
        entity_types=("PensionFundManager", "PointOfPresence", "CentralRecordkeepingAgency", "NPSTrust", "Custodian"),
        domains=("pension",),
        default_domain="pension",
        document_number_patterns=(_PFRDA_DOC_NUMBER,),
        source_tag_patterns=(re.compile(r"\bPFRDA\b", re.IGNORECASE), re.compile(r"pension fund regulatory and development authority", re.IGNORECASE)),
        agent_persona_hint=(
            "This clause is from a PFRDA (pension) circular or regulation governing the National Pension "
            "System. Typical entities are pension fund managers, points of presence, and the central "
            "recordkeeping agency; typical obligations involve investment exposure limits, fund switching "
            "rules, and subscriber disclosure requirements."
        ),
    ),
}


def detect_regulator_and_document(head_text: str, source_tag: str | None = None) -> tuple[Regulator, DocumentType, str | None]:
    """Determines (regulator, document_type, document_number) from the
    first page's text and/or an ingestion-time source tag (e.g. which
    feed URL a document was discovered from -- see
    app.ingestion.regulator_sources).

    Order of precedence: an explicit `source_tag` naming a known
    regulator's feed is trusted first (ingestion already knows which
    regulator's site it polled -- no need to re-derive that from OCR'd
    text that could contain a misleading cross-reference to a different
    regulator). Falls back to matching the document-number regex against
    the head text, then the looser source_tag_patterns against the head
    text itself. Defaults to (SEBI, CIRCULAR, None) when nothing matches,
    preserving this pipeline's original SEBI-only behavior for documents
    that genuinely don't declare themselves any other way.
    """
    if source_tag:
        for regulator, profile in REGULATOR_PROFILES.items():
            if regulator.value.lower() == source_tag.strip().lower():
                doc_type, doc_number = _match_document(profile, head_text)
                return regulator, doc_type, doc_number

    for regulator, profile in REGULATOR_PROFILES.items():
        for pattern in profile.document_number_patterns:
            if m := pattern.search(head_text):
                doc_type, _ = _match_document(profile, head_text)
                return regulator, doc_type, m.group(0).strip()

    for regulator, profile in REGULATOR_PROFILES.items():
        for pattern in profile.source_tag_patterns:
            if pattern.search(head_text):
                doc_type, doc_number = _match_document(profile, head_text)
                return regulator, doc_type, doc_number

    return Regulator.SEBI, DocumentType.CIRCULAR, None


def _match_document(profile: RegulatorProfile, head_text: str) -> tuple[DocumentType, str | None]:
    lowered = head_text.lower()
    for doc_type in profile.document_types:
        if doc_type.value.replace("_", " ") in lowered:
            return doc_type, None
    return profile.document_types[0], None


def resolve_domain(regulator: Regulator, entity_type: str | None) -> str:
    """Maps a resolved/normalized entity type to this regulator's Rego
    namespace domain segment (app.compiler.naming.rego_package_name).
    Falls back to the regulator's `default_domain` for an entity type not
    in its taxonomy (or no entity resolved at all) -- an unrecognized
    entity must still compile to SOME namespace rather than blocking
    compilation entirely; app.compiler.pipeline's HITL flagging is the
    right place to surface "this entity didn't normalize," not a
    KeyError here.
    """
    profile = REGULATOR_PROFILES[regulator]
    if not entity_type:
        return profile.default_domain

    entity_domain_hints: dict[str, str] = {
        "Stockbroker": "broking", "DepositoryParticipant": "depository", "AssetManagementCompany": "amc",
        "MutualFund": "amc", "PortfolioManager": "portfolio_management", "ResearchAnalyst": "research",
        "ScheduledCommercialBank": "banking", "CooperativeBank": "cooperative_banking", "SmallFinanceBank": "banking",
        "PaymentBank": "payments", "NBFC": "nbfc", "PaymentSystemOperator": "payments", "PrimaryDealer": "banking",
        "LifeInsurer": "underwriting", "GeneralInsurer": "underwriting", "HealthInsurer": "underwriting",
        "Reinsurer": "reinsurance", "InsuranceIntermediary": "distribution", "InsuranceBroker": "distribution", "TPA": "claims",
        "PensionFundManager": "pension", "PointOfPresence": "pension", "CentralRecordkeepingAgency": "pension",
        "NPSTrust": "pension", "Custodian": "pension",
    }
    domain = entity_domain_hints.get(entity_type)
    if domain and domain in profile.domains:
        return domain
    return profile.default_domain
