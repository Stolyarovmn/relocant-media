"""Export public observations as plain JSON without publishing anything."""

import argparse
import json
from pathlib import Path

from relocant_media.analytics.public_preview import PreviewError, collect_preview


def main() -> None:
    parser = argparse.ArgumentParser(description="Снимок публичного Telegram-канала")
    parser.add_argument("channel", help="Имя канала без @ и URL")
    parser.add_argument("--output", type=Path, help="Файл JSON вместо стандартного вывода")
    args = parser.parse_args()
    try:
        payload = collect_preview(args.channel)
        serialized = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        if args.output:
            args.output.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
    except PreviewError as exc:
        parser.exit(1, f"Ошибка: {exc}\n")
    except OSError:
        parser.exit(1, "Ошибка: не удалось сохранить снимок JSON\n")


if __name__ == "__main__":
    main()
