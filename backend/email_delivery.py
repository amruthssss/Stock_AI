"""Allowlisted, idempotent report delivery to local Mailpit or a dry-run log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.agent.checkpointing import enter_investigation_checkpointer
from backend.api.config import get_settings
from backend.api.database import create_database_engine
from backend.persistence.models import EmailDeliveryLog
from backend.reporting import render_report_email_html, render_report_email_text

MAX_ATTEMPTS = 3
PROCESSING_LEASE = timedelta(minutes=10)
RETRY_DELAYS_SECONDS = (0.5, 1.0)
LOCAL_SMTP_HOSTS = {"127.0.0.1", "localhost", "::1", "mailpit"}


class EmailDeliveryError(ValueError):
    """A report or recipient is invalid or unsafe to deliver."""


@dataclass(frozen=True)
class EmailSettings:
    """Local Mailpit settings; dry_run is the safe default."""

    mode: str = "dry_run"
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    from_address: str = "stockwise@localhost"
    allowed_recipients: tuple[str, ...] = ()
    timeout_seconds: float = 5.0

    @classmethod
    def from_environment(cls) -> EmailSettings:
        try:
            port = int(os.getenv("STOCKWISE_EMAIL_SMTP_PORT", "1025"))
            timeout = float(os.getenv("STOCKWISE_EMAIL_TIMEOUT_SECONDS", "5"))
        except ValueError as error:
            raise EmailDeliveryError("Email port and timeout must be numeric") from error
        recipients = tuple(
            value.strip().lower()
            for value in os.getenv("STOCKWISE_EMAIL_ALLOWED_RECIPIENTS", "").split(",")
            if value.strip()
        )
        return cls(
            mode=os.getenv("STOCKWISE_EMAIL_MODE", "dry_run").strip().lower(),
            smtp_host=os.getenv("STOCKWISE_EMAIL_SMTP_HOST", "127.0.0.1").strip(),
            smtp_port=port,
            from_address=os.getenv("STOCKWISE_EMAIL_FROM", "stockwise@localhost").strip(),
            allowed_recipients=recipients,
            timeout_seconds=timeout,
        )

    def validate(self) -> None:
        if self.mode not in {"dry_run", "mailpit"}:
            raise EmailDeliveryError("Email mode must be dry_run or mailpit")
        if not 1 <= self.smtp_port <= 65535 or not 1 <= self.timeout_seconds <= 30:
            raise EmailDeliveryError("SMTP port or timeout is outside the allowed range")
        if self.mode == "mailpit" and self.smtp_host.lower() not in LOCAL_SMTP_HOSTS:
            raise EmailDeliveryError("Only loopback SMTP hosts are allowed for local email delivery")
        if _valid_address(self.from_address) is None:
            raise EmailDeliveryError("Configured sender address is invalid")


def _valid_address(value: str) -> str | None:
    if "\r" in value or "\n" in value:
        return None
    _display_name, address = parseaddr(value)
    if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
        return None
    return address


def _load_report(report_path: Path, project_root: Path) -> dict:
    reports_root = (project_root / "data" / "processed" / "reports").resolve()
    path = Path(report_path).resolve()
    try:
        path.relative_to(reports_root)
    except ValueError as error:
        raise EmailDeliveryError("Report file must be inside data/processed/reports") from error
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmailDeliveryError("Report file is missing or invalid JSON") from error
    if not isinstance(report, dict):
        raise EmailDeliveryError("Report file must contain a JSON object")
    if report.get("report_type") not in {"daily_inventory", "weekly_data_science"}:
        raise EmailDeliveryError("Unsupported report type")
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or not report_id or len(report_id) > 64:
        raise EmailDeliveryError("Report ID is invalid")
    if not isinstance(report.get("summary"), dict):
        raise EmailDeliveryError("Report summary is missing")
    return report


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _reserve_delivery(
    engine: Engine,
    *,
    key: str,
    report_type: str,
    report_id: str,
    recipient: str,
    mode: str,
    now: datetime,
) -> tuple[str, int, str]:
    try:
        with Session(engine) as session, session.begin():
            record = session.scalar(
                select(EmailDeliveryLog)
                .where(EmailDeliveryLog.idempotency_key == key)
                .with_for_update()
            )
            if record is not None:
                if record.status in {"sent", "dry_run"}:
                    return record.delivery_id, record.attempt_count, "duplicate"
                if record.status == "processing" and now - _utc(record.updated_at_utc) < PROCESSING_LEASE:
                    return record.delivery_id, record.attempt_count, "in_progress"
                if record.attempt_count >= MAX_ATTEMPTS:
                    return record.delivery_id, record.attempt_count, "attempt_limit"
                record.status = "dry_run" if mode == "dry_run" else "processing"
                record.mode = mode
                record.error_category = None
                record.updated_at_utc = now
                return record.delivery_id, record.attempt_count, "reserved"

            record = EmailDeliveryLog(
                delivery_id=uuid.uuid4().hex,
                idempotency_key=key,
                report_type=report_type,
                report_id=report_id,
                recipient=recipient,
                mode=mode,
                status="dry_run" if mode == "dry_run" else "processing",
                attempt_count=0,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(record)
            session.flush()
            return record.delivery_id, record.attempt_count, "reserved"
    except IntegrityError:
        # A concurrent invocation won the unique idempotency-key insert.
        with Session(engine) as session:
            record = session.scalar(
                select(EmailDeliveryLog).where(EmailDeliveryLog.idempotency_key == key)
            )
            if record is None:
                raise
            return record.delivery_id, record.attempt_count, "in_progress"


def _update_delivery(
    engine: Engine,
    delivery_id: str,
    *,
    status: str,
    attempt_count: int,
    error_category: str | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        record = session.get(EmailDeliveryLog, delivery_id)
        if record is None:
            raise RuntimeError("Reserved email delivery record disappeared")
        record.status = status
        record.attempt_count = attempt_count
        record.error_category = error_category
        record.updated_at_utc = datetime.now(UTC)


def send_report_email(
    report_path: Path,
    recipient: str,
    *,
    project_root: Path,
    engine: Engine,
    settings: EmailSettings,
    idempotency_key_override: str | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Write a dry-run result or send once to local Mailpit with bounded retries."""
    settings.validate()
    report = _load_report(report_path, Path(project_root).resolve())
    normalized_recipient = _valid_address(recipient)
    if normalized_recipient is None or normalized_recipient.lower() not in {
        address.lower() for address in settings.allowed_recipients
    }:
        raise EmailDeliveryError("Recipient is not in the configured email allowlist")

    if idempotency_key_override is not None and (
        not idempotency_key_override or len(idempotency_key_override) > 160
    ):
        raise EmailDeliveryError("Scheduled idempotency key must contain between 1 and 160 characters")
    key_source = idempotency_key_override or f"{report['report_type']}:{report['report_id']}"
    idempotency_key = hashlib.sha256(
        f"{key_source}:{normalized_recipient.lower()}".encode()
    ).hexdigest()
    now = datetime.now(UTC)
    delivery_id, previous_attempts, reservation = _reserve_delivery(
        engine,
        key=idempotency_key,
        report_type=report["report_type"],
        report_id=report["report_id"],
        recipient=normalized_recipient,
        mode=settings.mode,
        now=now,
    )
    if reservation != "reserved":
        return {
            "delivery_id": delivery_id,
            "status": reservation,
            "attempt_count": previous_attempts,
            "error_category": None,
        }
    if settings.mode == "dry_run":
        return {"delivery_id": delivery_id, "status": "dry_run", "attempt_count": 0, "error_category": None}

    message = EmailMessage()
    report_title = (
        "Daily Inventory Report"
        if report.get("report_type") == "daily_inventory"
        else "Weekly Data Science Report"
    )
    message["Subject"] = f"StockWise | {report_title}"
    message["From"] = settings.from_address
    message["To"] = normalized_recipient
    message.set_content(render_report_email_text(report))
    message.add_alternative(render_report_email_html(report), subtype="html")

    attempts = previous_attempts
    for attempt_number in range(previous_attempts + 1, MAX_ATTEMPTS + 1):
        attempts = attempt_number
        _update_delivery(engine, delivery_id, status="processing", attempt_count=attempts)
        try:
            with smtp_factory(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.timeout_seconds,
            ) as smtp:
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            if attempt_number < MAX_ATTEMPTS:
                sleep(RETRY_DELAYS_SECONDS[attempt_number - 1])
                continue
            _update_delivery(
                engine,
                delivery_id,
                status="failed",
                attempt_count=attempts,
                error_category="smtp_error",
            )
            return {
                "delivery_id": delivery_id,
                "status": "failed",
                "attempt_count": attempts,
                "error_category": "smtp_error",
            }
        _update_delivery(engine, delivery_id, status="sent", attempt_count=attempts)
        return {"delivery_id": delivery_id, "status": "sent", "attempt_count": attempts, "error_category": None}

    return {"delivery_id": delivery_id, "status": "failed", "attempt_count": attempts, "error_category": "smtp_error"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or deliver a StockWise report safely.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--report-file", type=Path, help="send one saved report to --recipient")
    source.add_argument(
        "--kind",
        choices=("daily-inventory", "weekly-data-science"),
        help="generate the scheduled report and deliver to the configured allowlist",
    )
    parser.add_argument("--recipient", help="required with --report-file; must be allowlisted")
    arguments = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    api_settings = get_settings(project_root)
    settings = EmailSettings.from_environment()
    if not api_settings.database_url:
        parser.error("STOCKWISE_DATABASE_URL must be configured for persistent delivery logs")
    if arguments.report_file is not None and not arguments.recipient:
        parser.error("--recipient is required with --report-file")
    if arguments.kind is not None and not settings.allowed_recipients:
        parser.error("STOCKWISE_EMAIL_ALLOWED_RECIPIENTS must contain at least one recipient with --kind")
    engine = create_database_engine(api_settings.database_url)
    try:
        if arguments.kind is None:
            results = [
                send_report_email(
                    arguments.report_file,
                    arguments.recipient,
                    project_root=project_root,
                    engine=engine,
                    settings=settings,
                )
            ]
        else:
            from backend.reporting import (
                build_daily_inventory_report,
                build_weekly_data_science_report,
            )

            builder = (
                build_daily_inventory_report
                if arguments.kind == "daily-inventory"
                else build_weekly_data_science_report
            )
            with ExitStack() as stack:
                checkpointer = enter_investigation_checkpointer(engine, stack)
                report_path = builder(
                    project_root,
                    engine=engine,
                    checkpointer=checkpointer,
                    settings=api_settings,
                )
            scheduled_key = f"{arguments.kind}:{datetime.now(UTC).date().isoformat()}"
            results = [
                send_report_email(
                    report_path,
                    recipient,
                    project_root=project_root,
                    engine=engine,
                    settings=settings,
                    idempotency_key_override=scheduled_key,
                )
                for recipient in settings.allowed_recipients
            ]
    except (EmailDeliveryError, SQLAlchemyError, ValueError) as error:
        safe_message = (
            str(error)
            if isinstance(error, (EmailDeliveryError, ValueError))
            else "Email delivery log database is unavailable"
        )
        parser.error(safe_message)
    finally:
        engine.dispose()
    for result in results:
        print(
            f"Delivery {result['delivery_id']}: status={result['status']} "
            f"attempts={result['attempt_count']} error_category={result['error_category']}"
        )


if __name__ == "__main__":
    main()
