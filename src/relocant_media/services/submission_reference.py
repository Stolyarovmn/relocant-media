"""Stable, reader-facing numbers shared by submission messages."""

from secrets import choice

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import Submission

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PUBLIC_REFERENCE_PATTERN = rf"RM-[{_ALPHABET}]{{4}}-[{_ALPHABET}]{{4}}"


def new_submission_reference(session: Session) -> str:
    # The intake writer serializes creation. Resolve a random collision before saving.
    while True:
        token = "".join(choice(_ALPHABET) for _ in range(8))
        reference = f"RM-{token[:4]}-{token[4:]}"
        if session.scalar(select(Submission.id).where(
            Submission.payload["public_reference"].as_string() == reference,
        ).limit(1)) is None:
            return reference


def submission_reference(row: Submission) -> str:
    reference = row.payload.get("public_reference")
    return reference if isinstance(reference, str) and reference else str(row.id)
