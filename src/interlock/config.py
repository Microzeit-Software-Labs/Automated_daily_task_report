"""Application configuration.

Every business rule that could plausibly differ between deployments lives here
rather than in code. Nothing in this file is imported by the domain layer --
domain functions take the values they need as arguments, so they stay pure and
independently testable.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TaskSource(StrEnum):
    DATABASE = "database"
    GOOGLE_SHEETS = "google_sheets"
    EXCEL = "excel"


class WhatsAppProviderName(StrEnum):
    MOCK = "mock"
    LOCAL_AGENT = "local_agent"
    CLOUD_API = "cloud_api"


class SheetsProviderName(StrEnum):
    MOCK = "mock"
    GOOGLE = "google"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "development"
    database_url: str = "postgresql+psycopg://interlock_app:interlock@127.0.0.1:5432/interlock"

    # --- Timezone ---------------------------------------------------------
    timezone: str = "Asia/Kolkata"

    # --- Review schedule --------------------------------------------------
    morning_alert_time: dt.time = dt.time(9, 0)
    evening_alert_time: dt.time = dt.time(17, 0)
    # NoDecode: pydantic-settings otherwise tries to json.loads() any
    # "complex" (non-primitive) field's raw env string *before* our own
    # field_validator(mode="before") ever runs -- so a plain "0,1,2,3,4"
    # fails as invalid JSON at the settings-source layer, underneath and
    # earlier than our validator. NoDecode skips that source-level attempt
    # and hands the raw string straight to the validator below instead.
    working_days: Annotated[frozenset[int], NoDecode] = frozenset({0, 1, 2, 3, 4})
    skip_on_holidays: bool = True
    default_postpone_minutes: int = 5

    # --- Missed-window rule ----------------------------------------------
    send_grace_minutes: int = 60
    enforce_day_boundary: bool = True
    strict_data_drift: bool = False
    deferred_job_ttl_hours: int = 48

    # --- Scheduler --------------------------------------------------------
    scheduler_tick_seconds: int = 30
    claim_batch_size: int = Field(default=50, ge=1, le=500)
    claim_lease_seconds: int = 120

    # --- Retry ladder -----------------------------------------------------
    retry_delays_seconds: Annotated[tuple[int, ...], NoDecode] = (0, 30, 120, 600)

    # --- Integrations -----------------------------------------------------
    task_source: TaskSource = TaskSource.DATABASE
    whatsapp_provider: WhatsAppProviderName = WhatsAppProviderName.MOCK
    max_message_length: int = 4096

    # --- Google Sheets sync ------------------------------------------------
    # MOCK (an in-memory fake) is the safe default, exactly like
    # whatsapp_provider defaults to MOCK until the local agent ships -- the
    # sync tick always runs, harmlessly, until GOOGLE is configured below.
    sheets_provider: SheetsProviderName = SheetsProviderName.MOCK
    google_service_account_path: str | None = None
    google_sheets_spreadsheet_id: str | None = None
    google_sheets_sheet_name: str = "Tasks"
    sheet_sync_interval_seconds: int = 60

    # --- Feature flags ----------------------------------------------------
    allow_custom_send_time: bool = True
    allow_add_task: bool = True
    allow_inline_edit: bool = True

    # --- Idempotency ------------------------------------------------------
    idempotency_key_ttl_hours: int = 24

    # --- Default actor ------------------------------------------------------
    # Phase 1 has no login flow (see the Phase 1 plan's explicit scope cut --
    # auth UI is Phase 4). Every request acts as this one configured user,
    # identified via optional X-Actor-Id / X-Actor-Name headers with this as
    # the fallback. It is a placeholder, not a security boundary: anyone who
    # can reach the API can approve a share. That is acceptable for a
    # single-user tool running on its owner's own laptop and stops being
    # acceptable the moment this becomes multi-user.
    default_user_id: str = "u-default"
    default_user_name: str = "Kaif"

    # -- validators ---------------------------------------------------------

    @field_validator("working_days", mode="before")
    @classmethod
    def _parse_working_days(cls, value: object) -> object:
        """Accept "0,1,2,3,4" from the environment as well as a real set."""
        if isinstance(value, str):
            return frozenset(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("retry_delays_seconds", mode="before")
    @classmethod
    def _parse_retry_delays(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value

    @field_validator("working_days")
    @classmethod
    def _validate_working_days(cls, value: frozenset[int]) -> frozenset[int]:
        if not value:
            raise ValueError("WORKING_DAYS must contain at least one day")
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("WORKING_DAYS entries must be 0 (Monday) to 6 (Sunday)")
        return value

    @field_validator("retry_delays_seconds")
    @classmethod
    def _validate_retry_delays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("RETRY_DELAYS_SECONDS must contain at least one delay")
        if any(delay < 0 for delay in value):
            raise ValueError("RETRY_DELAYS_SECONDS entries must not be negative")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises if the zone is unknown
        return value

    # -- derived ------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def send_grace(self) -> dt.timedelta:
        return dt.timedelta(minutes=self.send_grace_minutes)

    @property
    def claim_lease(self) -> dt.timedelta:
        return dt.timedelta(seconds=self.claim_lease_seconds)

    @property
    def deferred_job_ttl(self) -> dt.timedelta:
        return dt.timedelta(hours=self.deferred_job_ttl_hours)

    @property
    def max_send_attempts(self) -> int:
        return len(self.retry_delays_seconds)

    def is_working_day(self, day: dt.date) -> bool:
        return day.weekday() in self.working_days


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
