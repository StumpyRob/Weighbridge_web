from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.ewc_import import import_ewc_codes


def main() -> None:
    parser = argparse.ArgumentParser(description="Import EWC codes from CSV.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=os.path.join(".", "data", "ewc_codes.csv"),
        help="Path to the EWC CSV file.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Deactivate codes that are not present in the uploaded CSV.",
    )
    args = parser.parse_args()

    csv_path = os.path.abspath(args.csv_path)
    if not os.path.exists(csv_path):
        raise SystemExit(f"CSV file not found: {csv_path}")

    result = import_ewc_codes(
        Path(csv_path),
        replace=bool(args.replace),
        source_name=os.path.basename(csv_path),
        imported_by="script",
    )
    if result.fatal_error:
        raise SystemExit(result.fatal_error)
    print(
        "Inserted: {inserted} | Updated: {updated} | Unchanged: {unchanged} | "
        "Skipped: {skipped} | Deactivated: {deactivated} | Errors: {errors}".format(
            inserted=result.inserted,
            updated=result.updated,
            unchanged=result.unchanged,
            skipped=result.skipped,
            deactivated=result.deactivated,
            errors=result.error_count,
        )
    )


if __name__ == "__main__":
    main()
