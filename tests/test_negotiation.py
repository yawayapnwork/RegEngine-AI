"""Tests for the multi-agent negotiation protocol (app.negotiation):
domain-agent scoping/voting, the deterministic weighted-consensus state
machine, the conflict arbiter's precedence rules, HITL escalation, and
the Redis-backed telemetry transcript store.

Follows this codebase's established test-double convention (see
tests/test_opa_execution.py, tests/test_agent_graph.py): small,
hand-rolled fakes implementing only the exact methods under test, no
fakeredis/MagicMock.
"""
from __future__ import annotations

import pytest

from app.execution.models import Decision, HITLCase, PolicyOutcome, TransactionPayload
from app.negotiation.agents import DomainAgent, FundSegregationAgent, MarginAgent, RiskDisclosureAgent
from app.negotiation.arbiter import ConflictArbiterAgent, build_reasoning_tree
from app.negotiation.consensus import ConsensusEngine, ConsensusState, check_consensus, tally_votes
from app.negotiation.models import AgentVerdict, NegotiationRound, NegotiationStatus, WeightedVoteTally
from app.negotiation.orchestrator import NegotiationOrchestrator, should_negotiate
from app.negotiation.telemetry import NegotiationTranscriptStore


def _txn(**overrides) -> TransactionPayload:
    defaults = dict(transaction_id="txn-1", entity_type="Stockbroker", facts={"margin_pct": 12.0})
    defaults.update(overrides)
    return TransactionPayload(**defaults)


def _outcome(**overrides) -> PolicyOutcome:
    defaults = dict(rule_id="rule-1", package="sebi.broking.circulars.x.clause_1", allow=True, violations=[])
    defaults.update(overrides)
    return PolicyOutcome(**defaults)


class TestDomainAgentScoping:
    @pytest.mark.asyncio
    async def test_margin_agent_abstains_when_nothing_in_scope(self) -> None:
        agent = MarginAgent()
        outcome = _outcome(violations=["Client risk disclosure statement was not provided before order placement."])
        verdict = await agent.evaluate(_txn(), [outcome])
        assert verdict is None

    @pytest.mark.asyncio
    async def test_margin_agent_denies_on_margin_violation(self) -> None:
        agent = MarginAgent()
        outcome = _outcome(
            allow=False, violations=["Upfront margin of 12% is below the required 20% minimum."],
            circular_number="SEBI/HO/MIRSD/2024/100", clause_number="4.2.b",
        )
        verdict = await agent.evaluate(_txn(), [outcome])
        assert verdict is not None
        assert verdict.decision == Decision.DENY
        assert verdict.agent_key == "margin_agent"
        assert verdict.cited_circular_number == "SEBI/HO/MIRSD/2024/100"

    @pytest.mark.asyncio
    async def test_fund_segregation_agent_flags_on_undefined_outcome(self) -> None:
        agent = FundSegregationAgent()
        outcome = _outcome(allow=None, package="sebi.broking.circulars.y.clause_client_fund_segregation")
        verdict = await agent.evaluate(_txn(), [outcome])
        assert verdict is not None
        assert verdict.decision == Decision.FLAGGED

    @pytest.mark.asyncio
    async def test_risk_disclosure_agent_allows_when_all_in_scope_pass(self) -> None:
        agent = RiskDisclosureAgent()
        outcome = _outcome(allow=True, violations=[], package="sebi.broking.circulars.z.clause_risk_disclosure")
        verdict = await agent.evaluate(_txn(), [outcome])
        assert verdict is not None
        assert verdict.decision == Decision.ALLOW

    @pytest.mark.asyncio
    async def test_confidence_softens_under_stronger_prior_round_dissent(self) -> None:
        agent = RiskDisclosureAgent()
        outcome = _outcome(allow=True, package="sebi.broking.circulars.z.clause_risk_disclosure")
        baseline = await agent.evaluate(_txn(), [outcome])

        margin_deny = AgentVerdict(
            agent_key="margin_agent", domain="margin", decision=Decision.DENY, confidence=0.9, weight=1.2,
            cited_circular_number="SEBI/HO/MIRSD/2024/100", cited_clause_number="4.2.b", rationale="Margin shortfall.",
        )
        prior_round = NegotiationRound(
            round_number=1, verdicts=[baseline, margin_deny],
            tally=tally_votes([baseline, margin_deny]), consensus_reached=False, dissenting_agents=["risk_disclosure_agent"],
        )

        softened = await agent.evaluate(_txn(), [outcome], prior_round=prior_round)
        assert softened.decision == baseline.decision  # decision label itself never changes
        assert softened.confidence < baseline.confidence


class TestConsensusTally:
    def test_unanimous_verdicts_reach_consensus(self) -> None:
        verdicts = [
            AgentVerdict(agent_key="a", domain="margin", decision=Decision.ALLOW, confidence=0.9, weight=1.0, rationale="ok"),
            AgentVerdict(agent_key="b", domain="risk_disclosure", decision=Decision.ALLOW, confidence=0.8, weight=1.0, rationale="ok"),
        ]
        tally = tally_votes(verdicts)
        assert tally.unanimous is True
        assert tally.leading_decision == Decision.ALLOW
        assert check_consensus(tally, threshold=0.7) is True

    def test_deny_tie_break_wins_close_split(self) -> None:
        verdicts = [
            AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.DENY, confidence=0.9, weight=1.2, rationale="deny"),
            AgentVerdict(agent_key="risk_disclosure_agent", domain="risk_disclosure", decision=Decision.ALLOW, confidence=0.85, weight=1.0, rationale="allow"),
        ]
        tally = tally_votes(verdicts)
        # DENY share (1.08) vs ALLOW share (0.85) out of 1.93 total -- DENY
        # already leads here without even needing the tie-break, so assert
        # the natural (safety-first) outcome directly.
        assert tally.leading_decision == Decision.DENY

    def test_below_threshold_does_not_reach_consensus(self) -> None:
        verdicts = [
            AgentVerdict(agent_key="a", domain="margin", decision=Decision.ALLOW, confidence=0.5, weight=1.0, rationale="ok"),
            AgentVerdict(agent_key="b", domain="fund_segregation", decision=Decision.DENY, confidence=0.5, weight=1.0, rationale="deny", cited_circular_number="X", cited_clause_number="1"),
        ]
        tally = tally_votes(verdicts)
        assert check_consensus(tally, threshold=0.9) is False


class _StaticAgent(DomainAgent):
    """Test double: always returns the same verdict regardless of round
    context -- used to force a clean, reproducible deadlock."""

    def __init__(self, agent_key: str, domain: str, weight: float, decision: Decision, confidence: float, keywords: tuple[str, ...],
                 circular: str | None = None, clause: str | None = None) -> None:
        self.agent_key = agent_key
        self.domain = domain
        self.weight = weight
        self.domain_keywords = keywords
        self._decision = decision
        self._confidence = confidence
        self._circular = circular
        self._clause = clause

    async def evaluate(self, transaction, policy_outcomes, prior_round=None):
        scoped = self.scoped_outcomes(policy_outcomes)
        if not scoped:
            return None
        return AgentVerdict(
            agent_key=self.agent_key, domain=self.domain, decision=self._decision, confidence=self._confidence,
            weight=self.weight, cited_circular_number=self._circular, cited_clause_number=self._clause,
            rationale=f"static {self._decision.value}", matched_rule_ids=[o.rule_id for o in scoped],
        )


@pytest.mark.asyncio
class TestConsensusEngine:
    async def test_reaches_consensus_on_first_round_when_agents_agree(self) -> None:
        agents = [
            _StaticAgent("margin_agent", "margin", 1.2, Decision.ALLOW, 0.9, ("margin",)),
            _StaticAgent("risk_disclosure_agent", "risk_disclosure", 1.0, Decision.ALLOW, 0.9, ("risk disclosure",)),
        ]
        outcomes = [
            _outcome(rule_id="r1", violations=[], package="sebi.broking.x.clause_margin"),
            _outcome(rule_id="r2", violations=[], package="sebi.broking.x.clause_risk_disclosure"),
        ]
        engine = ConsensusEngine(agents, consensus_threshold=0.7, max_rounds=3)
        rounds, state = await engine.run(_txn(), outcomes)
        assert state == ConsensusState.CONSENSUS_REACHED
        assert len(rounds) == 1
        assert rounds[0].tally.leading_decision == Decision.ALLOW

    async def test_deadlocks_after_max_rounds_on_persistent_split(self) -> None:
        agents = [
            _StaticAgent("margin_agent", "margin", 1.0, Decision.ALLOW, 0.5, ("margin",)),
            _StaticAgent("fund_segregation_agent", "fund_segregation", 1.0, Decision.DENY, 0.5, ("segregat",), circular=None, clause=None),
        ]
        outcomes = [
            _outcome(rule_id="r1", violations=[], package="sebi.broking.x.clause_margin"),
            _outcome(rule_id="r2", violations=["shortfall"], package="sebi.broking.x.clause_segregation"),
        ]
        engine = ConsensusEngine(agents, consensus_threshold=0.99, max_rounds=2)
        rounds, state = await engine.run(_txn(), outcomes)
        assert state == ConsensusState.DEADLOCKED
        assert len(rounds) == 2

    async def test_no_in_scope_agents_returns_empty_rounds(self) -> None:
        agents = [_StaticAgent("margin_agent", "margin", 1.0, Decision.ALLOW, 0.9, ("margin",))]
        engine = ConsensusEngine(agents, consensus_threshold=0.7, max_rounds=3)
        rounds, state = await engine.run(_txn(), [_outcome(package="sebi.broking.x.clause_unrelated_topic", violations=[])])
        assert rounds == []
        assert state == ConsensusState.DEADLOCKED


class TestArbiter:
    def test_explicit_clause_cited_deny_wins_outright(self) -> None:
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.9)
        verdicts = [
            AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.DENY, confidence=0.6, weight=1.2,
                         cited_circular_number="SEBI/HO/MIRSD/2024/100", cited_clause_number="4.2.b", rationale="Margin shortfall of 8%."),
            AgentVerdict(agent_key="risk_disclosure_agent", domain="risk_disclosure", decision=Decision.ALLOW, confidence=0.95, weight=1.0, rationale="Disclosure complete."),
        ]
        rounds = [NegotiationRound(round_number=1, verdicts=verdicts, tally=tally_votes(verdicts), consensus_reached=False, dissenting_agents=["risk_disclosure_agent"])]

        resolution = arbiter.arbitrate(rounds)
        assert resolution is not None
        assert resolution.resolved_decision == Decision.DENY
        assert resolution.precedence_rule == "EXPLICIT_DENY_PRECEDENCE"
        assert resolution.cited_clause_number == "4.2.b"

    def test_falls_back_to_highest_weight_confidence_when_no_explicit_deny(self) -> None:
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.5)
        verdicts = [
            AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.ALLOW, confidence=0.9, weight=1.2, rationale="Margin fine."),
            AgentVerdict(agent_key="fund_segregation_agent", domain="fund_segregation", decision=Decision.FLAGGED, confidence=0.4, weight=1.1, rationale="Undefined."),
        ]
        rounds = [NegotiationRound(round_number=1, verdicts=verdicts, tally=tally_votes(verdicts), consensus_reached=False, dissenting_agents=["fund_segregation_agent"])]

        resolution = arbiter.arbitrate(rounds)
        assert resolution is not None
        assert resolution.resolved_decision == Decision.ALLOW
        assert resolution.precedence_rule == "HIGHEST_WEIGHT_CONFIDENCE"

    def test_escalates_to_none_when_below_confidence_bar(self) -> None:
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.95)
        verdicts = [
            AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.ALLOW, confidence=0.5, weight=1.0, rationale="uncertain"),
            AgentVerdict(agent_key="fund_segregation_agent", domain="fund_segregation", decision=Decision.FLAGGED, confidence=0.5, weight=1.0, rationale="uncertain"),
        ]
        rounds = [NegotiationRound(round_number=1, verdicts=verdicts, tally=tally_votes(verdicts), consensus_reached=False, dissenting_agents=["margin_agent"])]

        assert arbiter.arbitrate(rounds) is None

    def test_reasoning_tree_includes_every_round_and_agent(self) -> None:
        verdicts = [AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.DENY, confidence=0.6, weight=1.2, rationale="x", cited_circular_number="C", cited_clause_number="1")]
        rounds = [NegotiationRound(round_number=1, verdicts=verdicts, tally=tally_votes(verdicts), consensus_reached=False, dissenting_agents=[])]
        tree = build_reasoning_tree(rounds)
        assert len(tree.children) == 1
        assert len(tree.children[0].children) == 1
        assert tree.children[0].children[0].supporting_agent_key == "margin_agent"


class _FakeHITLQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, transaction: TransactionPayload, reason: str, matched_policies: list[PolicyOutcome]) -> HITLCase:
        case = HITLCase(case_id=f"case-{len(self.enqueued) + 1}", transaction=transaction, reason=reason, matched_policies=matched_policies)
        self.enqueued.append({"transaction_id": transaction.transaction_id, "reason": reason})
        return case


class _NoOpTranscriptStore:
    def __init__(self) -> None:
        self.rounds: list = []
        self.outcomes: list = []

    async def record_round(self, negotiation_id, round_) -> None:
        self.rounds.append((negotiation_id, round_))

    async def record_outcome(self, result) -> None:
        self.outcomes.append(result)


@pytest.mark.asyncio
class TestOrchestrator:
    async def test_should_negotiate_requires_two_distinct_domains(self) -> None:
        agents = [MarginAgent(), RiskDisclosureAgent()]
        only_margin = [_outcome(package="sebi.broking.x.clause_margin", violations=[])]
        assert should_negotiate(only_margin, agents) is False

        both = only_margin + [_outcome(rule_id="r2", package="sebi.broking.x.clause_risk_disclosure", violations=[])]
        assert should_negotiate(both, agents) is True

    async def test_consensus_path_never_touches_hitl(self) -> None:
        agents = [
            _StaticAgent("margin_agent", "margin", 1.0, Decision.ALLOW, 0.9, ("margin",)),
            _StaticAgent("risk_disclosure_agent", "risk_disclosure", 1.0, Decision.ALLOW, 0.9, ("risk disclosure",)),
        ]
        engine = ConsensusEngine(agents, consensus_threshold=0.7, max_rounds=3)
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.75)
        hitl = _FakeHITLQueue()
        transcript = _NoOpTranscriptStore()
        orchestrator = NegotiationOrchestrator(agents, engine, arbiter, hitl, transcript)

        outcomes = [
            _outcome(rule_id="r1", package="sebi.broking.x.clause_margin", violations=[]),
            _outcome(rule_id="r2", package="sebi.broking.x.clause_risk_disclosure", violations=[]),
        ]
        result = await orchestrator.negotiate(_txn(), outcomes)

        assert result.status == NegotiationStatus.CONSENSUS
        assert result.final_decision == Decision.ALLOW
        assert hitl.enqueued == []
        assert len(transcript.outcomes) == 1

    async def test_arbitrated_path_resolves_without_hitl(self) -> None:
        agents = [
            _StaticAgent("margin_agent", "margin", 1.2, Decision.DENY, 0.6, ("margin",), circular="SEBI/HO/1/2024/1", clause="4.2.b"),
            _StaticAgent("risk_disclosure_agent", "risk_disclosure", 1.0, Decision.ALLOW, 0.95, ("risk disclosure",)),
        ]
        engine = ConsensusEngine(agents, consensus_threshold=0.99, max_rounds=1)
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.75)
        hitl = _FakeHITLQueue()
        transcript = _NoOpTranscriptStore()
        orchestrator = NegotiationOrchestrator(agents, engine, arbiter, hitl, transcript)

        outcomes = [
            _outcome(rule_id="r1", package="sebi.broking.x.clause_margin", violations=["shortfall"], circular_number="SEBI/HO/1/2024/1", clause_number="4.2.b"),
            _outcome(rule_id="r2", package="sebi.broking.x.clause_risk_disclosure", violations=[]),
        ]
        result = await orchestrator.negotiate(_txn(), outcomes)

        assert result.status == NegotiationStatus.ARBITRATED
        assert result.final_decision == Decision.DENY
        assert result.arbiter_resolution is not None
        assert hitl.enqueued == []

    async def test_deadlock_below_confidence_bar_escalates_to_hitl(self) -> None:
        agents = [
            _StaticAgent("margin_agent", "margin", 1.0, Decision.ALLOW, 0.5, ("margin",)),
            _StaticAgent("fund_segregation_agent", "fund_segregation", 1.0, Decision.FLAGGED, 0.5, ("segregat",)),
        ]
        engine = ConsensusEngine(agents, consensus_threshold=0.99, max_rounds=1)
        arbiter = ConflictArbiterAgent(min_confidence_to_resolve=0.95)
        hitl = _FakeHITLQueue()
        transcript = _NoOpTranscriptStore()
        orchestrator = NegotiationOrchestrator(agents, engine, arbiter, hitl, transcript)

        outcomes = [
            _outcome(rule_id="r1", package="sebi.broking.x.clause_margin", violations=[]),
            _outcome(rule_id="r2", package="sebi.broking.x.clause_segregation", allow=None),
        ]
        result = await orchestrator.negotiate(_txn(), outcomes)

        assert result.status == NegotiationStatus.ESCALATED_TO_HITL
        assert result.final_decision == Decision.FLAGGED
        assert result.hitl_case_id == "case-1"
        assert len(hitl.enqueued) == 1
        assert result.reasoning_tree is not None


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def rpush(self, key: str, value: str) -> "_FakePipeline":
        self._ops.append(("rpush", (key, value)))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self._ops.append(("expire", (key, seconds)))
        return self

    def hset(self, key: str, mapping: dict) -> "_FakePipeline":
        self._ops.append(("hset", (key, mapping)))
        return self

    async def execute(self) -> None:
        for op, args in self._ops:
            if op == "rpush":
                key, value = args
                self._redis.lists.setdefault(key, []).append(value)
            elif op == "hset":
                key, mapping = args
                self._redis.hashes.setdefault(key, {}).update(mapping)


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self):
        return _FakePipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : end + 1 if end != -1 else None]


@pytest.mark.asyncio
class TestNegotiationTranscriptStore:
    async def test_record_round_writes_transcript_and_summary(self) -> None:
        redis_client = _FakeRedis()
        store = NegotiationTranscriptStore(redis_client, key_prefix="regengine:negotiation", ttl_seconds=3600)
        verdicts = [AgentVerdict(agent_key="margin_agent", domain="margin", decision=Decision.ALLOW, confidence=0.9, weight=1.0, rationale="ok")]
        round_ = NegotiationRound(round_number=1, verdicts=verdicts, tally=tally_votes(verdicts), consensus_reached=True, dissenting_agents=[])

        await store.record_round("neg-1", round_)

        summary = await store.get_summary("neg-1")
        assert summary["last_round"] == "1"
        assert summary["consensus_reached"] == "True"

        transcript = await store.get_transcript("neg-1")
        assert len(transcript) == 1
        assert transcript[0]["round_number"] == 1
