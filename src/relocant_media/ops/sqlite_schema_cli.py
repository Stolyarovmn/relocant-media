"""Explicit SQLite maintenance without AI or Telegram configuration."""
import argparse
from pathlib import Path

from relocant_media.storage.sqlite_schema import initialize_sqlite


def main() -> None:
    parser = argparse.ArgumentParser(description="Подготовить/обновить SQLite, сохранив данные")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--legacy-utc-confirmed", action="store_true",
                        help="только после аудита, подтвердившего UTC всех старых дат")
    args = parser.parse_args()
    try:
        backup = initialize_sqlite(args.database, legacy_utc_confirmed=args.legacy_utc_confirmed)
    except Exception:
        parser.exit(1, "Схема не обновлена: проверьте файл, версию и резервную копию.\n")
    print("SQLite schema v1 готова" + (f"; резервная копия: {backup}" if backup else ""))


if __name__ == "__main__":
    main()
