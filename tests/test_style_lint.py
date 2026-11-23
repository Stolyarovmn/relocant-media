"""#313: замер матрицы подачи — подсказки по длине, абзацам и канцеляриту."""
import json
from datetime import UTC, datetime

import pytest
from test_channel_delivery import reviewed as reviewed_fixture

from relocant_media.ai.channel_style import word_range, writing_instruction
from relocant_media.domain.style_lint import count_words, lint
from relocant_media.ops import style_report
from relocant_media.storage.models import EditorialRun, Post

reviewed = reviewed_fixture


def _words(count, word="слово"):
    return " ".join([word] * count)


def test_word_range_follows_matrix_and_falls_back_to_city():
    assert word_range("transport") == (45, 100)
    assert word_range("unknown") == word_range("city") == (50, 110)
    assert "45–100 слов" in writing_instruction("transport")


def test_words_skip_urls_and_keep_hyphenated_and_numbers():
    assert count_words("Нови-Сад, 14 ноября: https://example.rs/a?b=1 см. www.x.rs") == 4


def test_range_flags_are_separate_hints():
    assert lint(_words(120), (50, 110)).over_range
    short = lint("Воду отключат завтра с 9:00 до 15:00 на Воеводинской.", (40, 100))
    # Короткий точный сервисный пост допустим матрицей: это подсказка, не дефект.
    assert short.under_range and not short.over_range and short.bureaucratese == {}


def test_long_plain_paragraph_is_flagged_but_lists_and_labels_are_not():
    labels = "\n".join(f"Где: {_words(12)}" for _ in range(5))
    bullets = "\n".join(f"- {_words(12)}" for _ in range(5))
    emoji_label = "Объезд: 🚧 " + _words(50)
    body = f"{_words(46)}\n\n{labels}\n\n{bullets}\n\n{emoji_label}\n\n{_words(45)}"
    assert lint(body, (50, 500)).long_paragraphs == 1


def test_bureaucratese_from_matrix_is_counted_and_ordinary_words_are_not():
    body = ("В связи с работами движение осуществляется по объезду. Данный участок является "
            "закрытым в целях безопасности, согласно информации ГСП. Подача воды будет "
            "приостановлена. По данным города, данные обновят.")
    assert lint(body, (10, 100)).bureaucratese == {
        "в связи с": 1, "осуществляется": 1, "данный": 1, "является": 1, "в целях": 1,
        "согласно информации": 1, "будет приостановлен": 1}


def _publish(session, run, *, category, body, when, **outcome):
    run = session.get(EditorialRun, run.raw_item_id)
    run.delivery_status = "sent"
    run.outcome = {**run.outcome, **outcome}
    post = session.get(Post, run.post_id)
    post.category, post.body = category, body
    post.telegram_message_id = 77
    post.published_at = when
    session.commit()


NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


def test_report_holds_only_aggregates_of_recent_editorial_posts(reviewed):
    session, run = reviewed
    secret = "Секретный заголовок и текст"
    _publish(session, run, category="transport", when=datetime(2026, 11, 22, tzinfo=UTC),
             body=f"{secret}. В связи с работами {_words(120)}")
    report = style_report.build_report(session, now=NOW, days=7)
    stats = report["categories"]["transport"]
    assert report["posts"] == 1 and report["matrix_version"] == "ru-local-news-v4"
    assert stats["over_range"] == 1 and stats["with_long_paragraph"] == 1
    assert stats["bureaucratese_terms"] == {"в связи с": 1}
    text = json.dumps(report, ensure_ascii=False) + style_report.markdown(report)
    assert secret not in text and "77" not in text
    assert "| transport | 1 | 45–100 |" in style_report.markdown(report)


def test_report_skips_old_and_reader_posts(reviewed):
    session, run = reviewed
    _publish(session, run, category="city", when=datetime(2026, 11, 1, tzinfo=UTC),
             body=_words(60))
    assert style_report.build_report(session, now=NOW, days=7)["posts"] == 0
    _publish(session, run, category="city", when=datetime(2026, 11, 22, tzinfo=UTC),
             body=_words(60), _reader_submission_id=5)
    report = style_report.build_report(session, now=NOW, days=7)
    assert report["posts"] == 0 and report["reader_posts_excluded"] == 1
    assert "Канцелярит: не найден" in style_report.markdown(report)


def test_report_days_are_bounded(reviewed):
    session, _ = reviewed
    with pytest.raises(ValueError):
        style_report.build_report(session, now=NOW, days=31)


def test_workflow_is_read_only_and_uploads_the_written_file():
    workflow = open(".github/workflows/style-report.yml", encoding="utf-8").read()
    assert "relocant_media.ops.style_report" in workflow
    assert '"$RUNNER_TEMP/style-report.json"' in workflow
    assert "path: ${{ runner.temp }}/style-report.json" in workflow
    assert "contents: read" in workflow and "contents: write" not in workflow
    assert "GEMINI_API_KEY" not in workflow and "TELEGRAM" not in workflow
