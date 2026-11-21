"""Cheap tripwire for untranslated prose; names are not a language verdict."""
import re

_SERBIAN = set("крајњи завршетак изградње пропратних саобраћајница градоначелник "
               "изјавио добијени локацијски године очекује током крену радови "
               "теренима такмичење препонском јахању бесплатан одржаће саопштено".split())


def _prose(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    # Short quoted proper names may retain Serbian spelling. Sentences do not.
    text = re.sub(r'[«„“"]([^»“”"\n]{1,70})[»“”"]',
                  lambda m: " " if len(m[1].split()) <= 6 and m[1][0].isupper()
                  and not any(w.casefold() in _SERBIAN for w in m[1].split()) else m[0], text)
    return text


# Letters that exist in Serbian Cyrillic but not in Russian. In a Russian post even a
# capitalised street name like «Војвођанска» reads as a typo and cannot be typed into a
# navigator, so names are transcribed to Russian with the Latin original in brackets.
SERBIAN_ONLY_CYRILLIC = re.compile(r"[јљњћђџЈЉЊЋЂЏ]")


def serbian_cyrillic_words(text: str) -> list[str]:
    text = re.sub(r"https?://\S+", "", text)
    return [word for word in re.findall(r"[^\W\d_]+", text) if SERBIAN_ONLY_CYRILLIC.search(word)]


def draft_language_reason(title: str, body: str) -> str | None:
    """Reject clear Serbian/Latin prose, never claim perfect language detection."""
    for text in (title.removeprefix("[тестовый запуск]"), body):
        if serbian_cyrillic_words(text):
            return "draft_requires_russian_translation"
        prose = _prose(text)
        words = re.findall(r"[^\W\d_]+", prose)
        if any(word.casefold() in _SERBIAN for word in words):
            return "draft_requires_russian_translation"
        # Preserve capitalized local street/venue names, but not Latin sentences.
        lower = " ".join(w for w in words if not (w[0].isupper() and re.search(r"[a-zA-Z]", w)))
        russian = len(re.findall(r"[а-яёА-ЯЁ]", lower))
        latin = len(re.findall(r"[a-zA-ZčćžšđČĆŽŠĐ]", lower))
        if not re.search(r"[а-яёА-ЯЁ]", prose) or latin > max(12, russian // 3):
            return "draft_requires_russian_translation"
    return None
