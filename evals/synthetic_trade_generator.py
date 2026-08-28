"""Synthetic SEBI Transaction Generator for Market Intermediary Order Streams.

Simulates high-volume, realistic stockbroker trade order flows and regulatory edge cases
based on the SEBI Master Circular for Stockbrokers (SEBI/HO/MRD/DP/CIR/P/2020/178 and
subsequent master circulars).

Scenarios Simulated:
  1. Upfront Margin Shortfall: Orders with upfront margin < 20% or uncollected peak margin.
  2. Unsegregated Client Funds: Client funds combined with proprietary broker accounts.
  3. Missing Risk Disclosure / KYC: Unsigned Risk Disclosure Documents or missing KRA verification.
  4. Excessive Intraday Leverage: Intraday leverage > 5x without haircut applications.
  5. Fully Compliant Baseline: 100% compliant market orders matching all SEBI norms.
  6. Mixed Market Stream: Realistic production distribution (80% compliant, 20% violations/flagged).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.execution.models import Decision, SourceChannel, TransactionPayload

logger = logging.getLogger("synthetic_trade_generator")


class ScenarioType(str, Enum):
    MARGIN_SHORTFALL = "upfront_margin_shortfall"
    UNSEGREGATED_FUNDS = "unsegregated_funds"
    MISSING_RISK_DISCLOSURE = "missing_risk_disclosure"
    EXCESSIVE_LEVERAGE = "excessive_leverage"
    FULLY_COMPLIANT = "fully_compliant"
    MIXED_MARKET_STREAM = "mixed_market_stream"


SAMPLE_BROKERS = [f"INZ{i:07d}" for i in range(1001, 1021)]
SAMPLE_SYMBOLS = ["RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK", "SBIN", "NIFTY_FUT", "BANKNIFTY_FUT"]
CLIENT_CATEGORIES = ["RETAIL", "HNI", "INSTITUTIONAL"]
ENTITY_TYPES = ["Stockbroker", "InvestmentAdviser", "PortfolioManager", "DepositoryParticipant"]


@dataclass
class SyntheticTrade:
    """A synthetic trade event paired with its ground truth expected verdict for evaluation scoring."""

    payload: dict[str, Any]
    expected_decision: str  # "allow", "deny", "flagged"
    expected_violations: list[str]
    scenario_name: str

    def to_transaction_payload(self) -> TransactionPayload:
        return TransactionPayload.model_validate(self.payload)


class SyntheticTradeGenerator:
    """Generates synthetic SEBI trade transactions with embedded ground truth labels."""

    def __init__(self, seed: int | None = None) -> None:
        if seed is not None:
            random.seed(seed)

    def generate_trade(
        self,
        trans_idx: int,
        scenario: ScenarioType,
    ) -> SyntheticTrade:
        if scenario == ScenarioType.MARGIN_SHORTFALL:
            return self._generate_margin_shortfall(trans_idx)
        elif scenario == ScenarioType.UNSEGREGATED_FUNDS:
            return self._generate_unsegregated_funds(trans_idx)
        elif scenario == ScenarioType.MISSING_RISK_DISCLOSURE:
            return self._generate_missing_risk_disclosure(trans_idx)
        elif scenario == ScenarioType.EXCESSIVE_LEVERAGE:
            return self._generate_excessive_leverage(trans_idx)
        elif scenario == ScenarioType.FULLY_COMPLIANT:
            return self._generate_fully_compliant(trans_idx)
        else:
            # Mixed realistic distribution
            rand_val = random.random()
            if rand_val < 0.80:
                return self._generate_fully_compliant(trans_idx)
            elif rand_val < 0.90:
                return self._generate_margin_shortfall(trans_idx)
            elif rand_val < 0.95:
                return self._generate_unsegregated_funds(trans_idx)
            else:
                return self._generate_missing_risk_disclosure(trans_idx)

    def generate_suite(
        self,
        count: int,
        scenario: ScenarioType = ScenarioType.MIXED_MARKET_STREAM,
    ) -> list[SyntheticTrade]:
        return [self.generate_trade(i + 1, scenario) for i in range(count)]

    def _base_payload(self, trans_idx: int, entity_type: str = "Stockbroker") -> tuple[dict[str, Any], str, str]:
        trans_id = f"SYN-TXN-{trans_idx:08d}-{int(time.time())}"
        broker_id = random.choice(SAMPLE_BROKERS)
        return trans_id, broker_id, entity_type

    def _generate_fully_compliant(self, trans_idx: int) -> SyntheticTrade:
        trans_id, broker_id, entity_type = self._base_payload(trans_idx)
        margin_pct = random.uniform(20.0, 50.0)

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": entity_type,
            "source_channel": SourceChannel.REST_SYNC.value,
            "facts": {
                "upfront_margin_pct": round(margin_pct, 2),
                "peak_margin_collected": True,
                "client_funds_segregated": True,
                "risk_disclosure_signed": True,
                "kyc_compliant": True,
                "client_category": random.choice(CLIENT_CATEGORIES),
                "order_value_inr": random.randint(50_000, 5_000_000),
                "collateral_haircut_applied": True,
                "intraday_leverage_ratio": round(random.uniform(1.0, 3.0), 2),
                "symbol": random.choice(SAMPLE_SYMBOLS),
            },
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        return SyntheticTrade(
            payload=payload,
            expected_decision=Decision.ALLOW.value,
            expected_violations=[],
            scenario_name=ScenarioType.FULLY_COMPLIANT.value,
        )

    def _generate_margin_shortfall(self, trans_idx: int) -> SyntheticTrade:
        trans_id, broker_id, entity_type = self._base_payload(trans_idx)
        # Violates 20% upfront margin requirement or peak margin
        shortfall_type = random.choice(["low_margin", "uncollected_peak"])
        
        if shortfall_type == "low_margin":
            margin_pct = random.uniform(2.0, 14.5)
            peak_collected = True
            reason = f"Upfront margin {margin_pct:.1f}% is below mandatory SEBI 20.0% threshold."
        else:
            margin_pct = 25.0
            peak_collected = False
            reason = "Peak margin collection flag is False."

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": entity_type,
            "source_channel": SourceChannel.REST_SYNC.value,
            "facts": {
                "upfront_margin_pct": round(margin_pct, 2),
                "peak_margin_collected": peak_collected,
                "client_funds_segregated": True,
                "risk_disclosure_signed": True,
                "kyc_compliant": True,
                "client_category": random.choice(CLIENT_CATEGORIES),
                "order_value_inr": random.randint(100_000, 10_000_000),
                "collateral_haircut_applied": True,
                "intraday_leverage_ratio": 2.0,
                "symbol": random.choice(SAMPLE_SYMBOLS),
            },
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        return SyntheticTrade(
            payload=payload,
            expected_decision=Decision.DENY.value,
            expected_violations=[reason],
            scenario_name=ScenarioType.MARGIN_SHORTFALL.value,
        )

    def _generate_unsegregated_funds(self, trans_idx: int) -> SyntheticTrade:
        trans_id, broker_id, entity_type = self._base_payload(trans_idx)

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": entity_type,
            "source_channel": SourceChannel.REST_SYNC.value,
            "facts": {
                "upfront_margin_pct": 25.0,
                "peak_margin_collected": True,
                "client_funds_segregated": False,  # SEBI Violation: unsegregated client funds
                "risk_disclosure_signed": True,
                "kyc_compliant": True,
                "client_category": random.choice(CLIENT_CATEGORIES),
                "order_value_inr": random.randint(500_000, 20_000_000),
                "collateral_haircut_applied": True,
                "intraday_leverage_ratio": 2.5,
                "symbol": random.choice(SAMPLE_SYMBOLS),
            },
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        return SyntheticTrade(
            payload=payload,
            expected_decision=Decision.DENY.value,
            expected_violations=["Client funds are not segregated from broker proprietary accounts."],
            scenario_name=ScenarioType.UNSEGREGATED_FUNDS.value,
        )

    def _generate_missing_risk_disclosure(self, trans_idx: int) -> SyntheticTrade:
        trans_id, broker_id, entity_type = self._base_payload(trans_idx)
        missing_type = random.choice(["rdd", "kyc"])

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": entity_type,
            "source_channel": SourceChannel.REST_SYNC.value,
            "facts": {
                "upfront_margin_pct": 30.0,
                "peak_margin_collected": True,
                "client_funds_segregated": True,
                "risk_disclosure_signed": (missing_type != "rdd"),
                "kyc_compliant": (missing_type != "kyc"),
                "client_category": random.choice(CLIENT_CATEGORIES),
                "order_value_inr": random.randint(100_000, 2_000_000),
                "collateral_haircut_applied": True,
                "intraday_leverage_ratio": 1.5,
                "symbol": random.choice(SAMPLE_SYMBOLS),
            },
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        return SyntheticTrade(
            payload=payload,
            expected_decision=Decision.FLAGGED.value,
            expected_violations=["Missing mandatory client risk disclosure (RDD) or KRA KYC verification."],
            scenario_name=ScenarioType.MISSING_RISK_DISCLOSURE.value,
        )

    def _generate_excessive_leverage(self, trans_idx: int) -> SyntheticTrade:
        trans_id, broker_id, entity_type = self._base_payload(trans_idx)
        leverage = round(random.uniform(6.5, 12.0), 2)

        payload = {
            "transaction_id": trans_id,
            "broker_id": broker_id,
            "entity_type": entity_type,
            "source_channel": SourceChannel.REST_SYNC.value,
            "facts": {
                "upfront_margin_pct": 20.0,
                "peak_margin_collected": True,
                "client_funds_segregated": True,
                "risk_disclosure_signed": True,
                "kyc_compliant": True,
                "client_category": "RETAIL",
                "order_value_inr": random.randint(1_000_000, 15_000_000),
                "collateral_haircut_applied": False,  # Missing haircut
                "intraday_leverage_ratio": leverage,  # > 5x limit
                "symbol": random.choice(SAMPLE_SYMBOLS),
            },
            "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        return SyntheticTrade(
            payload=payload,
            expected_decision=Decision.DENY.value,
            expected_violations=[f"Intraday leverage {leverage}x exceeds SEBI maximum allowable threshold (5.0x)."],
            scenario_name=ScenarioType.EXCESSIVE_LEVERAGE.value,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic SEBI Transaction Generator")
    parser.add_argument("--scenario", choices=[s.value for s in ScenarioType], default=ScenarioType.MIXED_MARKET_STREAM.value)
    parser.add_argument("--count", type=int, default=100, help="Number of synthetic trades to generate")
    parser.add_argument("--output-json", default=None, help="File path to export generated trades JSON")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")

    args = parser.parse_args()

    generator = SyntheticTradeGenerator(seed=args.seed)
    trades = generator.generate_suite(count=args.count, scenario=ScenarioType(args.scenario))

    logger.info("Generated %d synthetic trade events for scenario '%s'.", len(trades), args.scenario)

    if args.output_json:
        export_data = [asdict(t) for t in trades]
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2)
        logger.info("Exported synthetic trades to %s", args.output_json)


if __name__ == "__main__":
    main()
