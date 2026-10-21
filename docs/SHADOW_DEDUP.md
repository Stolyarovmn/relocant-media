# Shadow semantic dedup

Shadow dedup ищет возможные пропуски текущей детерминированной дедупликации, но **никогда**
не объединяет события и не влияет на публикацию. `same_event()` остаётся единственным
authoritative механизмом URL/content/event dedup.

## Зачем это нужно

Текущие правила намеренно консервативны: fuzzy title match применяется только между
разными источниками, в ограниченном окне и при высоком пороге. Это снижает риск ошибочно
склеить разные события, но может пропустить один повод в разных формулировках или в
сербской кириллице/латинице.

Shadow слой даёт измеряемый сигнал `possible_duplicate`, чтобы такие случаи сначала
размечать и считать false-positive/false-negative, а не сразу менять production policy.

## Baseline scorer

`LexicalSemanticScorer` не использует внешние AI API, embeddings или pgvector. Он сочетает:

- token overlap заголовков;
- token overlap текста;
- cosine similarity символьных 4-грамм;
- совпадение числовых якорей (даты, номера линий и т.п.);
- нормализацию регистра/диакритики и базовую транслитерацию сербской/части кириллицы.

Это локальный lexical-semantic baseline. Интерфейс `ShadowSemanticScorer` сделан pluggable:
если реальные размеченные данные покажут необходимость embeddings, новый scorer можно
подключить отдельно, не меняя `same_event()`.

Каждый scorer обязан объявить стабильный `scorer_id` и `scorer_version`. Embedding-backed
scorer дополнительно обязан объявить одновременно `embedding_model` и
`embedding_dimension`. Эти поля входят в каждый shadow signal/report и в результаты
`evaluate_shadow_labels()`. Сигналы из разных scorer spaces нельзя объединять в одной
оценке: evaluator завершится ошибкой вместо молчаливого смешивания несовместимых score.

Это важно до подключения embeddings: смена модели, размерности или алгоритма меняет само
пространство similarity, поэтому исторические precision/recall нельзя считать напрямую
сопоставимыми без явной версии scorer.

По умолчанию сравниваются только датированные материалы в окне 72 часа. Authoritative
duplicates исключаются из shadow report. Порог baseline — 0.68; это стартовая гипотеза,
а не доказанный production threshold.

## Использование

Обычный newsroom inbox не выполняет shadow scoring. Диагностика включается явно:

```bash
relocant-newsroom --database --city belgrade --shadow-duplicates --limit 100
```

В `shadow_duplicates` возвращаются:

- `mode=shadow_only`;
- `authoritative=false`;
- `decision_effect=none`;
- `scorer.id` / `scorer.version` и, для embeddings, model/dimension;
- пары raw item IDs/source IDs;
- общий score и threshold;
- компоненты score.

Наличие пары в этом списке **не** меняет state события, не блокирует доставку и не создаёт
новый review автоматически.

## Измерение качества

`ShadowDuplicateLabel` хранит внешнюю/тестовую разметку пары как duplicate/non-duplicate.
`evaluate_shadow_labels()` считает:

- scorer provenance;
- true/false positive;
- true/false negative;
- precision/recall;
- false-positive rate;
- false-negative rate.

Порог нельзя повышать до authoritative решения только потому, что несколько примеров
выглядят удачно. Сначала нужна достаточная размеченная выборка реальных материалов и
отдельная проверка same-source, recurring events, похожих транспортных уведомлений и
многоязычных формулировок.

## Инварианты

- без новых таблиц и миграций;
- без сетевых/AI вызовов;
- без записи в каноническую БД;
- без изменения event key, evidence bundle или delivery state;
- без Telegram side effects;
- deterministic dedup остаётся authoritative.
