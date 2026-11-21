"""#309: голос канала живой, но эмоции не проникают в чувствительные форматы."""
from relocant_media.ai.channel_style import (
    CHANNEL_DRAFT_STYLE,
    CHANNEL_STYLE_VERSION,
    writing_instruction,
)
from relocant_media.ai.prompts import DRAFT_SYSTEM


def test_voice_reaches_draft_and_review_prompts() -> None:
    assert CHANNEL_STYLE_VERSION == "ru-local-news-v4"
    assert "редактор, который сам живёт в этом городе" in CHANNEL_DRAFT_STYLE
    assert CHANNEL_DRAFT_STYLE in DRAFT_SYSTEM


def test_voice_keeps_evaluation_grounded_and_bounded() -> None:
    assert "одна короткая оценочная фраза" in CHANNEL_DRAFT_STYLE
    assert "не добавляет факт, совет, причину или обещание" in CHANNEL_DRAFT_STYLE
    assert "оценок и эмоций нет" in CHANNEL_DRAFT_STYLE
    assert "без панибратства" in CHANNEL_DRAFT_STYLE


def test_sensitive_formats_stay_restrained() -> None:
    for category in ("crime", "accusations", "politics", "law", "medicine", "immigration"):
        tone = writing_instruction(category)
        assert "Сдержанный" in tone or "Деловой" in tone, category


def test_old_ban_on_any_emotion_is_gone() -> None:
    assert "усиления эмоций" not in CHANNEL_DRAFT_STYLE
    assert "без эпитетов" not in writing_instruction("transport")
