"""Preview or persist an external editorial package; never send Telegram messages."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from relocant_media.ai.package import EditorialPackage, evaluate_package


class _DisabledAIProvider:
    def generate_json(
        self, *, task: str, system_instruction: str, prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        raise RuntimeError("package delivery must never invoke AI")


async def _persist(package: EditorialPackage, transport: str) -> dict[str, object]:
    from relocant_media.ai.pipeline import EditorialPipeline
    from relocant_media.bot.api import TelegramBotAPI
    from relocant_media.bot.publisher import StagingTelegramPublisher
    from relocant_media.config import settings
    from relocant_media.services.editorial import EditorialService
    from relocant_media.services.package_import import import_package
    from relocant_media.storage.db import SessionFactory

    if settings.app_env.lower() in {"prod", "production"}:
        raise ValueError("package import must not run in production")
    with SessionFactory() as session:
        run = import_package(session, package)
        result: dict[str, object] = {"mode": "persisted", "post_id": run.post_id}
        if run.outcome["status"] != "ready":
            return {**result, "delivery": "policy_blocked"}
        service = EditorialService(session, EditorialPipeline(_DisabledAIProvider()))
        preview = await service.deliver(package.raw_item_id, dry_run=True)
        result["delivery"] = asdict(preview)
        if transport == "fake":
            requests: list[dict[str, object]] = []

            def fake_send(request: httpx.Request) -> httpx.Response:
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "ok": True, "result": {"message_id": 1},
                })

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(fake_send), trust_env=False,
            ) as client:
                publisher = StagingTelegramPublisher(
                    TelegramBotAPI("fake-token", client), "-100000000001",
                )
                await publisher.publish(preview.text)
            result["fake_requests"] = requests
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an independently reviewed package")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--transport", choices=("dry-run", "fake"), default="dry-run")
    args = parser.parse_args()
    if args.transport == "fake" and not args.persist:
        parser.error("--transport fake requires --persist")
    package = EditorialPackage.model_validate_json(args.package.read_text(encoding="utf-8"))
    outcome = evaluate_package(package)
    result: dict[str, object] = {
        "mode": "preview", "raw_item_id": package.raw_item_id, "transport": args.transport,
        "outcome": outcome.model_dump(mode="json"),
        "text": f"{package.outcome.draft.title}\n\n{package.outcome.draft.body}"
                f"\n\nИсточник: {package.evidence.url}",
    }
    if args.persist:
        result.update(asyncio.run(_persist(package, args.transport)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
