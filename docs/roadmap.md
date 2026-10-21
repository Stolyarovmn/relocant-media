# План развития

Состояние на 21 октября 2026 года после PR #67.

## Этап 0 — основа и проверки

- [x] Архитектура, политика города и инженерные правила.
- [x] Реестр источников Белграда и детерминированные фикстуры коллекторов.
- [x] SQLAlchemy, Alembic и PostgreSQL/SQLite-путь с проверяемыми миграциями и переносом состояния.
- [x] Bot API dispatcher и идемпотентность входящих обновлений.
- [x] Event-level dedup, evidence bundle, source policy и повторная сверка доказательств перед доставкой.
- [x] `editorial_rule_version` в редакционных запусках/публикациях и разрез аналитики по версии правил.
- [ ] Продолжать проверку покрытия и актуальности реальных источников.

## Этап 1 — автономная серверная редакция

- [x] Сбор источников и сохранение raw items.
- [x] Gemini structured rank/claims/verify/draft с проверкой цитат и политики.
- [x] Две независимые stateless-проверки facts и utility полного пакета evidence v2.
- [x] Автоматический выбор свежих кандидатов и устойчивое окно без потери хвоста.
- [x] Детерминированный отсев старого/неподходящего до ИИ.
- [x] Ограниченный repair черновика с повторными независимыми ревью.
- [x] Source/category/factual/audience guards перед импортом и перед отправкой.
- [x] Сохранение `source_revision` при изменении уже проверенного первоисточника.
- [x] Реальная серверная установка systemd раз в три часа с общей блокировкой.
- [x] Gemini primary → подписочный ChatGPT fallback как внешний резервный процесс.
- [x] Проверяемые изображения/альбомы и выбор обложки из первоисточника.
- [x] Публичный режим канала без `[тестовый запуск]`, формируемый до ревью.
- [x] Защита доставки `sending/sent/uncertain` без автоматического повторения неопределённой отправки.
- [x] `cycle-audit` после серверного прохода.
- [x] Единый read-only newsroom state/control inbox поверх существующих `raw_item/editorial_run/post/source_revision/submission`, без второй очереди; см. [NEWSROOM_CONTROL.md](NEWSROOM_CONTROL.md).
- [x] Управляемый workflow повторного рассмотрения `source_revision`: append-only решение и причина видны в том же control plane; решение не снимает `needs_reverify`, требуется новый редакционный review. См. [SOURCE_REVISIONS.md](SOURCE_REVISIONS.md).

Подробности: [SERVER_EDITORIAL.md](SERVER_EDITORIAL.md), [EDITORIAL_CYCLE.md](EDITORIAL_CYCLE.md), [OPERATIONAL_STATE.md](OPERATIONAL_STATE.md).

## Этап 2 — предложка и сообщество

- [x] Создать основной канал, связанное обсуждение и отдельную форум-группу.
- [x] Включить и проверить анонимность владельца и отключение подписей сообщений канала.
- [x] Приём новостей/исправлений/рекламных заявок через Telegram-контур.
- [x] Приоритет новых предложений читателей и ограниченный поиск первоисточника.
- [x] Закрытые запросы уточнений при недостатке подтверждений.
- [x] Пятиминутные пачки предложки с одним производственным проходом на уникальное событие.
- [x] Не превращать частный текст/контакты/вложения в evidence или публичный search query.
- [ ] Проверить native DM с отдельного внешнего аккаунта в эксплуатационном контуре.
- [x] Читательские `/correction` имеют отдельный append-only статус/аудит редакционного решения в общем control plane без Telegram mutation и без публикации приватного текста; см. [READER_CORRECTIONS.md](READER_CORRECTIONS.md).
- [x] Детерминированная moderation pipeline v2: reason codes, high-confidence scam/spam delete, REVIEW для неоднозначной рекламы/harassment/flood hints, повторные ссылки, audit trail и application-level idempotency; см. [MODERATION.md](MODERATION.md).

## Этап 3 — аналитика и развитие правил

- [x] Хранение снимков постов/канала и daily report.
- [x] Scheduler с leases/retry и состоянием blocked для недоступных метрик.
- [x] Изолированный read-only MTProto adapter и fake-client тесты.
- [x] Публичные снимки конкурентов без MTProto с явными пропусками и приблизительными счётчиками.
- [x] Наблюдения 24/48/168 часов без синтетических нулей.
- [x] Версионирование редакционных правил и наблюдаемая производительность по версии.
- [ ] Накопить достаточное число сопоставимых собственных публикаций до каких-либо заявлений об обучении.
- [x] Shadow semantic dedup только как `possible_duplicate`: local pluggable scorer, отдельные score components и метрики false-positive/false-negative; deterministic dedup остаётся authoritative. См. [SHADOW_DEDUP.md](SHADOW_DEDUP.md).
- [x] Experiment registry: `experiment_id`, control/treatment, eligibility window, заранее заданная метрика, minimum samples, noise floor, concentration guard и явный rollback marker; см. [EXPERIMENTS.md](EXPERIMENTS.md).
- [x] Evaluation эксперимента детерминирована и read-only: LLM не объявляет победителя, `automatic_winner` всегда отсутствует, а factual/source-risk/safety rules исключены из experiment scope.
- [ ] Recommendation → issue/PR и проверка attribution/retained CPL на реальных данных.

MTProto не является условием работы редакции или этапа 1.

## Этап 4 — реклама

- [x] Хранение и приём advertiser requests через Bot API.
- [x] Базовый prompt review рекламного кандидата с запретом автоодобрения рискованных категорий.
- [x] Suggested-post review flow: append-only review точной версии `AdRequest.payload`, deterministic safety floor, privacy-safe control plane и обязательный owner approval; см. [SUGGESTED_POST_REVIEW.md](SUGGESTED_POST_REVIEW.md).
- [x] Fail-closed pricing/approval framework: никакой цены без явной rate card владельца; configured policy связывается с актуальным suggested-post review и exact policy hash; см. [AD_PRICING.md](AD_PRICING.md).
- [ ] Владелец должен явно задать реальные rate card: валюту, package IDs, base prices и допустимые bounds. В репозитории эти значения сейчас намеренно отсутствуют.
- [x] Read-only Telegram Ads analyst в отдельном trust domain: offline/imported snapshots → deterministic metrics → `proposed_only` recommendations, без spend/bid/budget mutation API; см. [ADS_ANALYST.md](ADS_ANALYST.md).
- [ ] Только после отдельной политики владельца рассматривать executor для bid/budget/spend; любые изменения денег остаются за `OWNER_APPROVAL` до явного bounded policy.
- [x] Synthetic staging dry-run рекламного процесса: `/ad` intake → candidate review → fail-closed pricing → read-only Ads analytics, без сети, Telegram mutation и денежных операций. При незаданной rate card ожидаемый результат — `owner_configuration_required`; см. [AD_STAGING_E2E.md](AD_STAGING_E2E.md).
