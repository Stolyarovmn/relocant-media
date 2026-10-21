"""Cheap rejection of ceremonial announcements without a resident-facing change."""
import re


def boilerplate_reason(title: str, body: str) -> str | None:
    heading = title.casefold()
    signing = re.search(r"potpis|потпис|подпис|signed", heading)
    document = re.search(r"protokol|протокол|memorandum|меморандум", heading)
    if not signing or not document:
        return None
    # A signing with a concrete effective change may still merit editorial review.
    impact = re.search(
        r"obustav|preusmer|izmen.*saobra|stupa.*snagu|pušt.*saobra|"
        r"обустав|преусмер|измен.*саобра|ступ.*силу|пушт.*саобра|"
        r"перекры|закрыт|измен.*движ|вступ.*силу|откро.*движ",
        (title + " " + body).casefold())
    return None if impact else "protocol_without_resident_change"
