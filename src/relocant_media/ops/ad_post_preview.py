"""Подготавливает платный пост для ручного согласования и отправки владельцем."""

import argparse
import re
from pathlib import Path

from relocant_media.domain.source_locations import is_public_https_url

_MARKDOWN = re.compile(r"([\\_*[\]()~\x60>#+\-=|{}.!])")


def _inline(value: str) -> str:
    if not value.strip() or "\n" in value or "\r" in value:
        raise ValueError("expected a non-empty single line")
    return _MARKDOWN.sub(r"\\\1", value.strip())


def paid_post_markdown(
    *, advertiser: str, headline: str, body: str, action: str, action_url: str,
) -> str:
    if not is_public_https_url(action_url) or any(c.isspace() for c in action_url):
        raise ValueError("advertisement requires a public HTTPS action")
    lines = "\n".join(_inline(line) if line.strip() else "" for line in body.strip().splitlines())
    if not lines or len(lines) > 2400:
        raise ValueError("advertisement body must be present and fit Telegram")
    url = action_url.replace("\\", "\\\\").replace(")", "\\)")
    return (f"*Реклама*\n{_inline(advertiser)}\n\n*{_inline(headline)}*\n\n{lines}\n\n"
            f"[{_inline(action)}]({url})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advertiser", required=True)
    parser.add_argument("--headline", required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--action-url", required=True)
    args = parser.parse_args()
    print(paid_post_markdown(advertiser=args.advertiser, headline=args.headline,
                            body=args.body_file.read_text(encoding="utf-8"),
                            action=args.action, action_url=args.action_url))


if __name__ == "__main__":
    main()
