"""Admin API endpoints for external data source health monitoring and on-demand probes."""

import logging
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, status
from sqlmodel import Session, select

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.core.config import settings
from kidcarematch_backend.cron.sync_centres import (
    ECDA_DATASTORE_SOURCE_ID,
    ECDA_DATASTORE_SOURCE_NAME,
)
from kidcarematch_backend.db.db import (
    ExternalDataSourceStatus,
    SyncState,
    User,
    get_session,
)
from kidcarematch_backend.schemas.admin import DataSourceStatusResponse

logger = logging.getLogger("admin_status")

ONEMAP_API_SOURCE_ID = "ONEMAP_API"
ONEMAP_API_SOURCE_NAME = "OneMap Geocoding & Mapping API"

router = APIRouter()


def get_or_create_status(
    session: Session,
    source_id: str,
    source_name: str,
) -> ExternalDataSourceStatus:
    """Retrieve existing status record or initialize default record in IDLE state."""
    status_rec = session.get(ExternalDataSourceStatus, source_id)
    if not status_rec:
        status_rec = ExternalDataSourceStatus(
            source_id=source_id,
            source_name=source_name,
            sync_state=SyncState.IDLE,
            records_processed=0,
        )
        session.add(status_rec)
        session.commit()
        session.refresh(status_rec)
    return status_rec


def probe_ecda_datastore(client: httpx.Client) -> tuple[bool, str | None]:
    """Perform a lightweight HTTP probe against data.gov.sg datastore search endpoint."""
    try:
        response = client.get(
            settings.DATA_GOV_SG_API_URL,
            params={"resource_id": settings.DATA_GOV_SG_CENTRES_RESOURCE_ID, "limit": 1},
        )
        response.raise_for_status()
        return True, None
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        logger.warning("ECDA datastore probe failed: %s", exc)
        return False, str(exc)[:1000]


def probe_onemap_api(client: httpx.Client) -> tuple[bool, str | None]:
    """Perform a lightweight HTTP probe against OneMap API public search endpoint."""
    try:
        response = client.get(
            settings.ONEMAP_API_URL,
            params={"searchVal": "singapore", "returnGeom": "N", "getAddrDetails": "N", "pageNum": 1},
        )
        response.raise_for_status()
        return True, None
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        logger.warning("OneMap API probe failed: %s", exc)
        return False, str(exc)[:1000]


@router.get(
    "",
    response_model=list[DataSourceStatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Get External Data Source Statuses",
)
@router.get(
    "/",
    response_model=list[DataSourceStatusResponse],
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_external_data_source_statuses(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(get_current_admin)],
) -> list[ExternalDataSourceStatus]:
    """Retrieve the latest operational heartbeat records for all monitored data sources."""
    get_or_create_status(session, ECDA_DATASTORE_SOURCE_ID, ECDA_DATASTORE_SOURCE_NAME)
    get_or_create_status(session, ONEMAP_API_SOURCE_ID, ONEMAP_API_SOURCE_NAME)

    all_statuses = session.exec(
        select(ExternalDataSourceStatus).order_by(ExternalDataSourceStatus.source_id)
    ).all()
    return list(all_statuses)


@router.post(
    "/refresh",
    response_model=list[DataSourceStatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Refresh External Data Source Health Probes",
)
def refresh_external_data_source_statuses(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[User, Depends(get_current_admin)],
) -> list[ExternalDataSourceStatus]:
    """Perform on-demand probes against external providers and update heartbeats."""
    ecda = get_or_create_status(session, ECDA_DATASTORE_SOURCE_ID, ECDA_DATASTORE_SOURCE_NAME)
    onemap = get_or_create_status(session, ONEMAP_API_SOURCE_ID, ONEMAP_API_SOURCE_NAME)

    now = datetime.now(timezone.utc)

    with httpx.Client(timeout=10.0) as client:
        ecda_ok, ecda_err = probe_ecda_datastore(client)
        onemap_ok, onemap_err = probe_onemap_api(client)

    ecda.last_attempted_sync = now
    if ecda_ok:
        ecda.sync_state = SyncState.SUCCESS
        ecda.last_successful_sync = now
        ecda.error_message = None
    else:
        ecda.sync_state = SyncState.FAILED
        ecda.error_message = ecda_err

    onemap.last_attempted_sync = now
    if onemap_ok:
        onemap.sync_state = SyncState.SUCCESS
        onemap.last_successful_sync = now
        onemap.error_message = None
    else:
        onemap.sync_state = SyncState.FAILED
        onemap.error_message = onemap_err

    session.add(ecda)
    session.add(onemap)
    session.commit()
    session.refresh(ecda)
    session.refresh(onemap)

    all_statuses = session.exec(
        select(ExternalDataSourceStatus).order_by(ExternalDataSourceStatus.source_id)
    ).all()
    return list(all_statuses)
