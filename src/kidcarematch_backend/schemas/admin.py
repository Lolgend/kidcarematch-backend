"""Admin request and response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kidcarematch_backend.db.db import AccountStatus, SyncState, UserRole


class AdminProfileResponse(BaseModel):
    """Administrator profile response schema."""

    user_id: str
    email: str
    full_name: str
    role: UserRole
    account_status: AccountStatus
    admin_level: int | None = Field(default=None)

    model_config = ConfigDict(from_attributes=True)


class AdminLogoutResponse(BaseModel):
    """Administrator logout response schema."""

    message: str = "Successfully logged out"


class AdminSyncResponse(BaseModel):
    """Response schema for asynchronous dataset synchronization trigger."""

    status: SyncState = SyncState.RUNNING
    message: str = "ECDA synchronization started in background"
    source_id: str = "ECDA_DATASTORE"


class DataSourceStatusResponse(BaseModel):
    """Operational heartbeat record response schema for monitored external data sources."""

    source_id: str
    source_name: str
    sync_state: SyncState
    last_successful_sync: datetime | None = None
    last_attempted_sync: datetime | None = None
    records_processed: int = 0
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)
