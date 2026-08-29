"""Process-wide singletons for the breach-notification engine, mirroring
app.execution.dependencies' pattern (PolicyCache there / connection
manager here both MUST be one instance per process since their purpose is
holding state across requests -- a fresh instance per request would hold
zero connections)."""
from __future__ import annotations

from functools import lru_cache

from app.incident.websocket_manager import BreachDashboardConnectionManager


@lru_cache(maxsize=1)
def get_dashboard_connection_manager() -> BreachDashboardConnectionManager:
    return BreachDashboardConnectionManager()
