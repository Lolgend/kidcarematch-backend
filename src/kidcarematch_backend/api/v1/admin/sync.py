"""Admin API endpoints for dataset synchronization triggers."""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlmodel import Session

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.cron.sync_centres import (
    ECDA_DATASTORE_SOURCE_ID,
    ECDA_DATASTORE_SOURCE_NAME,
    sync_centres,
    update_data_source_status,
)
from kidcarematch_backend.db.db import SyncState, User, get_session
from kidcarematch_backend.schemas.admin import AdminSyncResponse

logger = logging.getLogger("admin_sync")

router = APIRouter()


def run_sync_centres(db_engine: Any = None) -> None:
    """Execute background sync pipeline catching and logging exceptions."""
    try:
        sync_centres(db_engine=db_engine)
    except Exception:
        logger.exception("Background execution of sync_centres failed")


@router.post(
    "/ecda",
    response_model=AdminSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger ECDA Datastore Sync",
)
def trigger_ecda_sync(
    background_tasks: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(get_current_admin)],
) -> AdminSyncResponse:
    """Asynchronously trigger full preschool centres, vacancies, and services sync."""
    db_engine = session.get_bind()

    # Eagerly update operational heartbeat to RUNNING with start timestamp
    update_data_source_status(
        db_engine=db_engine,
        source_id=ECDA_DATASTORE_SOURCE_ID,
        source_name=ECDA_DATASTORE_SOURCE_NAME,
        sync_state=SyncState.RUNNING,
        last_attempted_sync=datetime.now(timezone.utc),
    )

    background_tasks.add_task(run_sync_centres, db_engine=db_engine)

    return AdminSyncResponse(
        status=SyncState.RUNNING,
        message="ECDA synchronization started in background",
        source_id=ECDA_DATASTORE_SOURCE_ID,
    )
