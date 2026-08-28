"""Interactive Webhook & Callback API Routes for Slack and MS Teams.

Handles:
  1. Slack Interactive Button Callbacks (`POST /v1/webhooks/slack/actions`)
     - Verifies HMAC-SHA256 signature (`X-Slack-Signature`).
     - Updates PostgreSQL `HITLReview` state (`RESOLVED` / `REJECTED`).
     - Triggers OPA policy hot-reloading via `PolicyPublisher`.
  2. MS Teams Adaptive Card Callbacks (`POST /v1/webhooks/teams/actions`)
     - Handles Teams Action.Submit payloads, updates DB, hot-reloads OPA.
  3. Pipeline Notification Trigger (`POST /v1/webhooks/trigger-flagged-notification`)
     - Dispatches real-time Slack/Teams alerts when new qualitative/ambiguous rules are flagged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import CompiledRule, HITLReview
from app.db.session import get_db_session
from app.execution.dependencies import get_policy_publisher
from app.execution.policy_publisher import PolicyPublisher
from app.webhooks.notifier import AuditNotifier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks-callbacks"])


class NotificationTriggerRequest(BaseModel):
    review_id: str
    circular_name: str
    clause_number: str | None = None
    source_excerpt: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reason_code: str = "qualitative_directive"
    severity: str = "blocking"


def verify_slack_signature(
    body_bytes: bytes,
    timestamp: str | None,
    signature: str | None,
    signing_secret: str,
) -> bool:
    """Verifies Slack HMAC-SHA256 request signature (`v0=...`) to prevent spoofing & replay attacks."""
    if not timestamp or not signature or not signing_secret:
        return False

    # Check timestamp staleness (prevent replay attacks > 5 min old)
    try:
        req_timestamp = int(timestamp)
        now_timestamp = int(dt.datetime.now(dt.timezone.utc).timestamp())
        if abs(now_timestamp - req_timestamp) > 300:
            logger.warning("Slack signature verification failed: timestamp stale (%ds diff)", abs(now_timestamp - req_timestamp))
            return False
    except ValueError:
        return False

    sig_basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}".encode("utf-8")
    computed_hmac = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_hmac, signature)


@router.post("/slack/actions", status_code=status.HTTP_200_OK)
async def slack_interactive_action_callback(
    request: Request,
    payload: str = Form(None),
    x_slack_request_timestamp: str | None = Header(None, alias="X-Slack-Request-Timestamp"),
    x_slack_signature: str | None = Header(None, alias="X-Slack-Signature"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    policy_publisher: PolicyPublisher = Depends(get_policy_publisher),
) -> Response:
    """Handles Slack interactive button clicks (Approve Policy / Modify AST / Reject).

    Verifies HMAC-SHA256 signature, updates PostgreSQL `HITLReview` status, records the officer's Slack identity,
    and publishes a `PolicyEvent` so OPA server hot-reloads the policy immediately upon approval.
    """
    body_bytes = await request.body()

    # 1. Verify Slack HMAC Signature if signing secret configured
    if settings.slack_signing_secret:
        if not verify_slack_signature(body_bytes, x_slack_request_timestamp, x_slack_signature, settings.slack_signing_secret):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or stale Slack HMAC signature.")

    # Parse Slack payload (Slack sends payload as a URL-encoded form parameter)
    if payload:
        try:
            payload_data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON in Slack payload.") from exc
    else:
        try:
            payload_data = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing or malformed payload.")

    actions = payload_data.get("actions", [])
    if not actions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No action specified in Slack payload.")

    first_action = actions[0]
    action_id = first_action.get("action_id")
    review_id = first_action.get("value")
    slack_user = payload_data.get("user", {}).get("username") or payload_data.get("user", {}).get("name") or "slack_compliance_officer"

    if not review_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing review_id in Slack action value.")

    # 2. Fetch HITLReview entry from database
    result = await session.execute(select(HITLReview).where(HITLReview.review_id == review_id))
    review = result.scalar_one_or_none()
    if review is None:
        return Response(
            content=json.dumps({"text": f"⚠️ Review `{review_id}` not found in database or already deleted."}),
            media_type="application/json",
        )

    now_utc = dt.datetime.now(dt.timezone.utc)
    formatted_time = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    # 3. Handle Action Execution
    if action_id == "approve_policy":
        if review.status == "RESOLVED":
            return Response(
                content=json.dumps({"text": f"ℹ️ Review `{review_id}` was already APPROVED by `{review.compliance_officer_id}`."}),
                media_type="application/json",
            )

        compiled_rule: CompiledRule | None = None
        if review.compiled_rule_id is not None:
            compiled_rule = await session.get(CompiledRule, review.compiled_rule_id)
            if compiled_rule:
                # Deactivate older versions of same rule_id
                await session.execute(
                    CompiledRule.__table__.update()
                    .where(CompiledRule.rule_id == compiled_rule.rule_id, CompiledRule.id != compiled_rule.id)
                    .values(is_active=False)
                )
                compiled_rule.is_active = True
                compiled_rule.hitl_status = "RESOLVED"

        review.status = "RESOLVED"
        review.compliance_officer_id = f"slack:{slack_user}"
        review.resolution_notes = f"Approved via Slack interactive button at {formatted_time}"
        review.resolved_at = now_utc

        await session.commit()
        await session.refresh(review)
        logger.info("HITL review '%s' APPROVED via Slack by '%s'", review_id, slack_user)

        # Trigger OPA hot-reload via PolicyPublisher
        if compiled_rule:
            try:
                await policy_publisher.publish_approved(compiled_rule, approved_by=f"slack:{slack_user}")
            except Exception as pub_exc:
                logger.warning("DB approval committed, but OPA hot-reload publish lagged: %s", pub_exc)

        response_text = f"✅ *Policy APPROVED* by @{slack_user} via Slack on {formatted_time}. OPA Policy hot-swapped!"

    elif action_id == "reject_policy":
        review.status = "REJECTED"
        review.compliance_officer_id = f"slack:{slack_user}"
        review.resolution_notes = f"Rejected via Slack interactive button at {formatted_time}"
        review.resolved_at = now_utc

        await session.commit()
        await session.refresh(review)
        logger.info("HITL review '%s' REJECTED via Slack by '%s'", review_id, slack_user)
        response_text = f"❌ *Policy REJECTED* by @{slack_user} via Slack on {formatted_time}."

    elif action_id == "modify_ast":
        portal_url = f"https://compliance.regengine.internal/reviews/{review_id}/edit"
        response_text = f"✏️ *AST Editor Initiated* by @{slack_user}. <{portal_url}|Click here to open the Interactive Rego/AST Visual Editor>."

    else:
        response_text = f"Unrecognized action `{action_id}`."

    # Return updated message back to Slack to replace the interactive card in-channel
    slack_response = {
        "replace_original": True,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": response_text},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Review ID: `{review_id}` | Operator: `slack:{slack_user}`"}],
            },
        ],
    }

    return Response(content=json.dumps(slack_response), media_type="application/json")


@router.post("/teams/actions", status_code=status.HTTP_200_OK)
async def teams_adaptive_card_action_callback(
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
    policy_publisher: PolicyPublisher = Depends(get_policy_publisher),
) -> dict[str, Any]:
    """Handles MS Teams Adaptive Card `Action.Submit` callbacks."""
    action = payload.get("action")
    review_id = payload.get("review_id")
    teams_user = payload.get("user", {}).get("name") or "teams_compliance_officer"

    if not action or not review_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing action or review_id in Teams payload.")

    result = await session.execute(select(HITLReview).where(HITLReview.review_id == review_id))
    review = result.scalar_one_or_none()
    if review is None:
        return {"text": f"Review '{review_id}' not found."}

    now_utc = dt.datetime.now(dt.timezone.utc)
    formatted_time = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

    if action == "approve_policy":
        compiled_rule: CompiledRule | None = None
        if review.compiled_rule_id is not None:
            compiled_rule = await session.get(CompiledRule, review.compiled_rule_id)
            if compiled_rule:
                await session.execute(
                    CompiledRule.__table__.update()
                    .where(CompiledRule.rule_id == compiled_rule.rule_id, CompiledRule.id != compiled_rule.id)
                    .values(is_active=False)
                )
                compiled_rule.is_active = True
                compiled_rule.hitl_status = "RESOLVED"

        review.status = "RESOLVED"
        review.compliance_officer_id = f"teams:{teams_user}"
        review.resolution_notes = f"Approved via MS Teams Adaptive Card at {formatted_time}"
        review.resolved_at = now_utc

        await session.commit()
        if compiled_rule:
            try:
                await policy_publisher.publish_approved(compiled_rule, approved_by=f"teams:{teams_user}")
            except Exception:
                pass
        msg = f"✅ Policy APPROVED by {teams_user} via MS Teams."

    elif action == "reject_policy":
        review.status = "REJECTED"
        review.compliance_officer_id = f"teams:{teams_user}"
        review.resolution_notes = f"Rejected via MS Teams at {formatted_time}"
        review.resolved_at = now_utc
        await session.commit()
        msg = f"❌ Policy REJECTED by {teams_user} via MS Teams."

    else:
        msg = f"AST Modification link: https://compliance.regengine.internal/reviews/{review_id}/edit"

    return {"type": "message", "text": msg}


@router.post("/trigger-flagged-notification", status_code=status.HTTP_200_OK)
async def trigger_flagged_rule_notification(
    req: NotificationTriggerRequest,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Pipeline trigger endpoint: dispatches real-time Slack and Teams alerts for a flagged compliance rule."""
    notifier = AuditNotifier(
        slack_webhook_url=settings.slack_webhook_url,
        teams_webhook_url=settings.teams_webhook_url,
    )

    results = await notifier.notify_flagged_rule(
        review_id=req.review_id,
        circular_name=req.circular_name,
        clause_number=req.clause_number,
        source_excerpt=req.source_excerpt,
        confidence_score=req.confidence_score,
        reason_code=req.reason_code,
        severity=req.severity,
    )

    return {
        "review_id": req.review_id,
        "dispatched": results,
        "status": "notification_sent" if any(results.values()) else "no_webhook_configured",
    }
