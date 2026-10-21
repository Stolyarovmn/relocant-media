# Newsroom control inbox

`relocant-newsroom --database` строит read-only event-level проекцию состояния редакции
из канонической БД. Это не новая очередь и не отдельный источник истины: команда читает
существующие `raw_item`, `editorial_run`, `post`, `source_revision`, `submission` и
`collector_failure`.

Старый режим сводки одного серверного запуска сохранён:

```bash
python -m relocant_media.ops.newsroom_status --runs /var/lib/relocant-media/runs
```

Состояние всей текущей редакционной очереди:

```bash
relocant-newsroom --database --city belgrade --limit 100
```

## Состояния

- `discovered` — сырой материал сохранён, но fingerprints ещё не готовы;
- `evidence_ready` — canonical URL/content hash готовы, редакционный запуск ещё не создан;
- `drafting` — явно сохранённое промежуточное состояние подготовки;
- `review` — пакет требует review по текущей политике;
- `ready` — проверенный пакет готов к доставке;
- `sending` — claim доставки уже сохранён; автоматический повтор запрещён;
- `sent` — доставка зафиксирована;
- `held` — материал остановлен правилом/решением;
- `needs_reverify` — после редакционного решения появилась новая `source_revision`;
- `expired` — сохранённая причина показывает, что повод просрочен;
- `failed` — редакционный outcome явно зафиксирован как failed/error;
- `uncertain` — исход Bot API неизвестен, требуется ручная сверка, повтор запрещён.

Порядок в inbox поднимает наверх состояния, требующие действия: `uncertain`,
`needs_reverify`, `sending`, `failed`, `review`, затем `ready` и новые материалы.

## Содержимое строки события

Проекция объединяет детерминированно совпадающие raw items в событие и показывает:

- `event_key`, anchor и все `raw_item_ids`;
- состояние и машинный `reason_code`;
- review/delivery status и `editorial_rule_version`;
- только безопасную сводку source policy (`ownership`, `high_risk`, `auto_publish`, `enabled`);
- IDs документов evidence и количество документов;
- количество наблюдавшихся source revisions;
- связанные submission ID/kind/status без текста, контактов и вложений;
- последнюю техническую ошибку источника без полного diagnostic message;
- точную retry policy: `manual_reconciliation`, `fresh_review_required`,
  `scheduler_or_operator` или `none`.

## Инварианты

- Команда не создаёт и не изменяет `EditorialRun`, `Post`, `Submission` или delivery receipt.
- `sending/uncertain` никогда не объявляются автоматически retryable.
- Новая source revision после review поднимает событие в `needs_reverify`, даже если старое
  решение было положительным.
- Private submission payload не входит в control output; показываются только безопасные
  метаданные связи.
- Проекция не ослабляет event dedup, source/category/audience/factual guards и не отправляет
  сообщения Telegram.
