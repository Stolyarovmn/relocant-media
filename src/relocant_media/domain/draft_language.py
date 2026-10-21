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


def draft_language_reason(title: str, body: str) -> str | None:
    """Reject clear Serbian/Latin prose, never claim perfect language detection."""
    for text in (title.removeprefix("[тестовый запуск]"), body):
        prose = _prose(text)
        words = re.findall(r"[^\W\d_]+", prose)
        if any(word.casefold() in _SERBIAN for word in words):
            return "draft_requires_russian_translation"
        if any(re.search(r"[јљњћђџ]", word) and not word[0].isupper() for word in words):
            return "draft_requires_russian_translation"
        # Preserve capitalized local street/venue names, but not Latin sentences.
        lower = " ".join(w for w in words if not (w[0].isupper() and re.search(r"[a-zA-Z]", w)))
        russian = len(re.findall(r"[а-яёА-ЯЁ]", lower))
        latin = len(re.findall(r"[a-zA-ZčćžšđČĆŽŠĐ]", lower))
        if not re.search(r"[а-яёА-ЯЁ]", prose) or latin > max(12, russian // 3):
            return "draft_requires_russian_translation"
    return None
