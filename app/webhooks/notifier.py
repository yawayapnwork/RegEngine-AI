"""Slack Block Kit & MS Teams Adaptive Card Interactive Notification Builder.

Constructs rich interactive notification payloads for ambiguous or qualitative rules
flagged by the Extraction Agent and Logic Auditor Agent, enabling compliance officers
to perform one-click approvals, AST modifications, or rejections directly from Slack/Teams.

Supported Integrations:
  - Slack Block Kit (interactive buttons with callback action_ids)
  - MS Teams Adaptive Cards v1.4 (ActionSet with Action.Submit payloads)
"""

from __future__ import annotations

import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)


class InteractiveNotificationBuilder:
    """Constructs structured, rich interactive payloads for Slack and Teams."""

    @staticmethod
    def build_slack_block_kit(
        review_id: str,
        circular_name: str,
        clause_number: str | None,
        source_excerpt: str,
        confidence_score: float,
        reason_code: str,
        severity: str = "blocking",
        portal_url: str | None = None,
    ) -> dict[str, Any]:
        """Constructs a Slack Block Kit payload with embedded interactive buttons."""
        clause_str = clause_number or "N/A"
        confidence_pct = f"{confidence_score * 100:.1f}%"
        portal_link = portal_url or f"https://compliance.regengine.internal/reviews/{review_id}"

        # Severity indicator emoji
        severity_emoji = "🚨" if severity == "blocking" else "⚠️"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{severity_emoji} Logic Auditor Alert: Ambiguous Compliance Rule Flagged",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"The **Logic Auditor Agent** flagged a rule in **{circular_name}** "
                        f"requiring human compliance review before policy activation."
                    ),
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Circular Name:*\n{circular_name}"},
                    {"type": "mrkdwn", "text": f"*Clause Number:*\n`{clause_str}`"},
                    {"type": "mrkdwn", "text": f"*Reason Code:*\n`{reason_code}`"},
                    {"type": "mrkdwn", "text": f"*Agent Confidence:*\n`{confidence_pct}`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Raw Source Clause Excerpt:*\n>{source_excerpt}",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Review ID: `{review_id}` | Severity: `{severity.upper()}` | <{portal_link}|Open Review Portal>",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": f"hitl_approval_{review_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve Policy", "emoji": True},
                        "style": "primary",
                        "value": review_id,
                        "action_id": "approve_policy",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✏️ Modify AST", "emoji": True},
                        "value": review_id,
                        "action_id": "modify_ast",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                        "style": "danger",
                        "value": review_id,
                        "action_id": "reject_policy",
                    },
                ],
            },
        ]

        return {"text": f"Logic Auditor Alert: Rule in {circular_name} flagged for review", "blocks": blocks}

    @staticmethod
    def build_teams_adaptive_card(
        review_id: str,
        circular_name: str,
        clause_number: str | None,
        source_excerpt: str,
        confidence_score: float,
        reason_code: str,
        severity: str = "blocking",
    ) -> dict[str, Any]:
        """Constructs an MS Teams Adaptive Card v1.4 payload with Action.Submit buttons."""
        clause_str = clause_number or "N/A"
        confidence_pct = f"{confidence_score * 100:.1f}%"

        card_content = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": "🚨 Logic Auditor Alert: Ambiguous Rule Flagged",
                    "weight": "Bolder",
                    "size": "Medium",
                    "color": "Attention" if severity == "blocking" else "Warning",
                },
                {
                    "type": "TextBlock",
                    "text": f"The Extraction & Logic Auditor Agent flagged an ambiguous or qualitative clause in **{circular_name}**.",
                    "wrap": True,
                },
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": "Circular:", "value": circular_name},
                        {"title": "Clause Number:", "value": clause_str},
                        {"title": "Reason Code:", "value": reason_code},
                        {"title": "Confidence Score:", "value": confidence_pct},
                        {"title": "Review ID:", "value": review_id},
                    ],
                },
                {
                    "type": "Container",
                    "style": "emphasis",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": f"**Raw Clause Excerpt:**\n_{source_excerpt}_",
                            "wrap": True,
                        }
                    ],
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "✅ Approve Policy",
                    "style": "positive",
                    "data": {"action": "approve_policy", "review_id": review_id},
                },
                {
                    "type": "Action.Submit",
                    "title": "✏️ Modify AST",
                    "data": {"action": "modify_ast", "review_id": review_id},
                },
                {
                    "type": "Action.Submit",
                    "title": "❌ Reject",
                    "style": "destructive",
                    "data": {"action": "reject_policy", "review_id": review_id},
                },
            ],
        }

        # Return MS Teams connector webhook envelope format
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": card_content,
                }
            ],
        }


class AuditNotifier:
    """Notification service sending Slack and Teams notifications via HTTP webhooks."""

    def __init__(
        self,
        slack_webhook_url: str | None = None,
        teams_webhook_url: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.slack_webhook_url = slack_webhook_url
        self.teams_webhook_url = teams_webhook_url
        self.timeout = timeout_seconds

    async def notify_flagged_rule(
        self,
        review_id: str,
        circular_name: str,
        clause_number: str | None,
        source_excerpt: str,
        confidence_score: float,
        reason_code: str,
        severity: str = "blocking",
    ) -> dict[str, bool]:
        """Dispatches interactive notification messages to Slack and MS Teams webhooks concurrently."""
        results = {"slack": False, "teams": False}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 1. Dispatch Slack Webhook
            if self.slack_webhook_url:
                try:
                    slack_payload = InteractiveNotificationBuilder.build_slack_block_kit(
                        review_id=review_id,
                        circular_name=circular_name,
                        clause_number=clause_number,
                        source_excerpt=source_excerpt,
                        confidence_score=confidence_score,
                        reason_code=reason_code,
                        severity=severity,
                    )
                    resp = await client.post(self.slack_webhook_url, json=slack_payload)
                    if resp.status_code in (200, 201, 204):
                        results["slack"] = True
                        logger.info("✅ Slack notification sent for HITL review '%s'", review_id)
                    else:
                        logger.warning("Slack webhook returned status %d: %s", resp.status_code, resp.text)
                except Exception as exc:
                    logger.warning("Failed to send Slack webhook for review '%s': %s", review_id, exc)

            # 2. Dispatch MS Teams Webhook
            if self.teams_webhook_url:
                try:
                    teams_payload = InteractiveNotificationBuilder.build_teams_adaptive_card(
                        review_id=review_id,
                        circular_name=circular_name,
                        clause_number=clause_number,
                        source_excerpt=source_excerpt,
                        confidence_score=confidence_score,
                        reason_code=reason_code,
                        severity=severity,
                    )
                    resp = await client.post(self.teams_webhook_url, json=teams_payload)
                    if resp.status_code in (200, 201, 204):
                        results["teams"] = True
                        logger.info("✅ MS Teams notification sent for HITL review '%s'", review_id)
                    else:
                        logger.warning("MS Teams webhook returned status %d: %s", resp.status_code, resp.text)
                except Exception as exc:
                    logger.warning("Failed to send MS Teams webhook for review '%s': %s", review_id, exc)

        return results
