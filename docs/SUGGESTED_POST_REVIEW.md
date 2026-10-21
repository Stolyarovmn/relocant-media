# Suggested-post review для рекламных заявок

Рекламная заявка из `/ad` остаётся частным `AdRequest`. Текст и Telegram identity рекламодателя
не публикуются в newsroom control plane. Этот workflow только оценивает предложенный пост и не
имеет доступа к публикации, Telegram Ads, bid, budget или spend.

## Два уровня решения

Reviewer (человек или AI-компонент) формирует `proposed_outcome`:

- `accept_candidate`;
- `counter_offer_candidate`;
- `human_review`;
- `reject`.

Приложение затем независимо вычисляет `effective_outcome` и может только сделать решение более
консервативным. Hard safety floor работает без доверия к reviewer verdict:

- `scam` или `illegal_goods` → `reject`;
- `medicine`, `investment`, `credit`, `gambling`, `political`, `unverifiable_earnings` →
  `human_review`;
- `accept_candidate` без подтверждённого audience fit → `human_review`;
- `accept_candidate` с непроверенными material claims → `human_review`.

Даже `effective_outcome=accept_candidate` **не является разрешением на публикацию**. В каждой
записи `requires_owner_approval=true`. Отдельного publish action в этом модуле нет.

## Связь review с точной версией заявки

Перед записью считается SHA256 канонического JSON `AdRequest.payload`. Review хранит этот hash.
Если payload позже изменился, `relocant-newsroom --database` показывает review как `stale`; старый
вердикт нельзя считать относящимся к новой версии текста.

История append-only. Повтор идентичного вызова с тем же `idempotency_key` безопасен; использовать
тот же ключ для другого review запрещено.

## Control plane

`relocant-newsroom --database` добавляет `suggested_post_review_control` с:

- количеством всех заявок и заявок, которым ещё нужен review;
- intake status;
- статусом `unreviewed`, `stale`, `human_review_required` или `candidate_reviewed`;
- последним proposed/effective outcome;
- reason codes, risk flags, audience-fit и claims-verification flags;
- признаком обязательного owner approval.

В control output намеренно отсутствуют private request text, Telegram user/chat ID и контактные
данные рекламодателя.

## Явная запись review

```powershell
relocant-ad-review `
  --ad-request-id 15 `
  --proposed-outcome accept_candidate `
  --reason-code local_service_relevant `
  --audience-fit yes `
  --claims-verified yes `
  --reviewer ad-review-agent `
  --idempotency-key ad-15-review-v1 `
  --confirm-write
```

Для рискованной категории reviewer передаёт `--risk-flag`, например `investment`. Application
safety floor всё равно переведёт попытку `accept_candidate` в `human_review`.

Без `--confirm-write` БД не изменяется.

## Границы

- Никакого Telegram send/edit/delete.
- Никакого Telegram Ads API/cookie/session доступа.
- Никакого price approval или списания денег.
- `counter_offer_candidate` не создаёт и не отправляет коммерческое предложение автоматически.
- `reject`/`human_review` не меняют исходный `AdRequest.status`; review history хранится отдельно.
- Реальная публикация и коммерческие условия должны быть отдельными bounded workflows.
