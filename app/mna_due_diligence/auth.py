"""Authorization and tenant-isolation enforcement for M&A Compliance Due-Diligence.

CRITICAL SECURITY REQUIREMENT:
Entity A data and Entity B data must never be exposed to an unauthorized user or accidentally mixed.
Access to one entity NEVER grants or infers access to another entity.
"""
from __future__ import annotations

import logging
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tenant
from app.security.models import Principal, Role

logger = logging.getLogger(__name__)


async def verify_dual_entity_authorization(
    principal: Principal,
    entity_a_id: str,
    entity_b_id: str,
    db: AsyncSession,
    explicit_authorized_entities: Sequence[str] | None = None,
) -> None:
    """Verifies that the requesting principal has explicit permission to access BOTH entities.

    Invariants:
    1. A single-tenant Broker_API_Client whose token is scoped to entity_a CANNOT
       compare against entity_b. Access to one entity NEVER infers access to another.
    2. Compliance_Officer and System_Admin have enterprise oversight to conduct
       cross-entity due-diligence comparisons across registered entities.
    3. Multi-entity machine tokens must carry explicit authorized_entities claims
       covering both entity_a_id and entity_b_id.
    4. Both entities must exist as active, registered tenants in the database.

    Raises:
        HTTPException(403): If the principal lacks permission to access both entities.
        HTTPException(404): If either entity is not registered in the system.
        HTTPException(400): If either entity is deactivated.
    """
    if not entity_a_id or not entity_b_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Both entity_a_id and entity_b_id must be specified.",
        )

    # 1. Check Principal Permissions
    has_dual_access = False

    if principal.is_admin() or Role.COMPLIANCE_OFFICER in principal.roles:
        # Human compliance officers and system admins have authorized cross-tenant audit role
        has_dual_access = True
    elif Role.BROKER_API_CLIENT in principal.roles:
        # Machine client: check if token possesses multi-entity claims
        authorized_set = set(explicit_authorized_entities or [])
        if principal.tenant_id:
            authorized_set.add(principal.tenant_id)

        # Access to entity_a does NOT grant access to entity_b!
        if entity_a_id in authorized_set and entity_b_id in authorized_set:
            has_dual_access = True
        else:
            logger.warning(
                "Cross-entity authorization failed for principal %s (tenant=%s) requesting %s vs %s",
                principal.subject,
                principal.tenant_id,
                entity_a_id,
                entity_b_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Cross-entity authorization failed. Permission to access both entities is required; "
                    "access to one entity does not infer authorization for another."
                ),
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Principal lacks required role for M&A due-diligence comparison.",
        )

    if not has_dual_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unauthorized: dual-entity permission required.",
        )

    # 2. Verify entities exist and are active in the database
    stmt = select(Tenant).where(Tenant.tenant_id.in_([entity_a_id, entity_b_id]))
    result = await db.execute(stmt)
    found_tenants = {t.tenant_id: t for t in result.scalars().all()}

    if entity_a_id not in found_tenants:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Regulated entity '{entity_a_id}' not found in registry.",
        )
    if entity_b_id not in found_tenants:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Regulated entity '{entity_b_id}' not found in registry.",
        )

    for eid, tenant in found_tenants.items():
        if not tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Regulated entity '{eid}' is deactivated; cannot perform due-diligence.",
            )
