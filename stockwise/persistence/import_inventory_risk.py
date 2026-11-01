"""Command-line entry point to import one completed inventory-risk report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from stockwise.api.config import get_settings
from stockwise.api.database import create_database_engine
from stockwise.persistence.inventory_risks import (
    import_inventory_risk_report,
    load_inventory_risk_report,
)


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Import one completed inventory-risk report into PostgreSQL.")
    parser.add_argument(
        "--file",
        required=True,
        help="path to a JSON report under data/processed/inventory_risks/runs",
    )
    args = parser.parse_args()

    try:
        record = load_inventory_risk_report(Path(args.file), project_root=project_root)
        database_url = get_settings().database_url
        if not database_url:
            raise ValueError("STOCKWISE_DATABASE_URL is not configured")
        engine = create_database_engine(database_url)
        try:
            result = import_inventory_risk_report(record, engine=engine)
        finally:
            engine.dispose()
    except ValueError as error:
        print(f"Inventory-risk import rejected: {error}", file=sys.stderr)
        return 1
    except (OSError, SQLAlchemyError) as error:
        print(f"Inventory-risk import failed ({type(error).__name__}).", file=sys.stderr)
        return 1

    print(f"Inventory-risk run {record.run['run_id']}: {result}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
