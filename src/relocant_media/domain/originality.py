"""Publication text must be our own retelling, never a run of the source's words."""

import re
from collections.abc import Iterable

# Ten consecutive identical words is a copied sentence, not a shared name or number.
COPIED_RUN_WORDS = 10


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def copies_source_text(
    draft: str, sources: Iterable[str], *, run_words: int = COPIED_RUN_WORDS,
) -> bool:
    draft_words = _words(draft)
    if len(draft_words) < run_words:
        return False
    runs = {
        tuple(draft_words[start:start + run_words])
        for start in range(len(draft_words) - run_words + 1)
    }
    for source in sources:
        words = _words(source)
        if any(
            tuple(words[start:start + run_words]) in runs
            for start in range(len(words) - run_words + 1)
        ):
            return True
    return False
