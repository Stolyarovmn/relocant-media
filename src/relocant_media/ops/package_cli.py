"""Preview or persist an external editorial package; never send Telegram messages."""

import argparse
import json
from pathlib import Path

from relocant_media.ai.package import EditorialPackage, evaluate_package


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an independently reviewed package")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    package = EditorialPackage.model_validate_json(args.package.read_text(encoding="utf-8"))
    outcome = evaluate_package(package)
    result: dict[str, object] = {
        "mode": "preview", "raw_item_id": package.raw_item_id,
        "outcome": outcome.model_dump(mode="json"),
        "text": f"{package.outcome.draft.title}\n\n{package.outcome.draft.body}"
                f"\n\nИсточник: {package.evidence.url}",
    }
    if args.persist:
        from relocant_media.config import settings
        from relocant_media.services.package_import import import_package
        from relocant_media.storage.db import SessionFactory

        if settings.app_env.lower() in {"prod", "production"}:
            raise ValueError("package import must not run in production")
        with SessionFactory() as session:
            run = import_package(session, package)
            result.update(mode="persisted", post_id=run.post_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
