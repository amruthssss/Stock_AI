"""Explicitly download missing M5 competition CSVs when invoked as a CLI."""

from __future__ import annotations

from pathlib import Path


FILES_TO_DOWNLOAD = (
    "calendar.csv",
    "sales_train_validation.csv",
    "sell_prices.csv",
)


def download_m5_files(data_directory: Path | None = None) -> list[Path]:
    """Download missing M5 files into data_directory; importing this module is inert."""
    import kagglehub

    project_root = Path(__file__).resolve().parents[2]
    if data_directory is None:
        data_directory = project_root / "data" / "raw"
    data_directory = Path(data_directory)
    data_directory.mkdir(parents=True, exist_ok=True)

    downloaded_files: list[Path] = []
    for file_name in FILES_TO_DOWNLOAD:
        target_path = data_directory / file_name
        if target_path.exists():
            print(f"{file_name}: already exists at {target_path}; skipping")
            downloaded_files.append(target_path)
            continue

        downloaded_path = kagglehub.competition_download(
            "m5-forecasting-accuracy",
            path=file_name,
            output_dir=str(data_directory),
        )
        downloaded_files.append(Path(downloaded_path))
        print(f"{file_name}: {downloaded_path}")
    return downloaded_files


def main() -> int:
    download_m5_files()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
