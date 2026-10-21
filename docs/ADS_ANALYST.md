# Read-only Telegram Ads analyst

Рекламная аналитика отделена от newsroom и от любого будущего исполнителя, который
может менять деньги. Текущая реализация умеет только читать заранее экспортированный
снимок кампаний, считать детерминированные метрики и формировать предложения.

## Trust domain

`relocant_media.ads` не получает Bot API, MTProto, Telegram Ads cookies/session или
другие send/mutation credentials. Контракт `AdsReadOnlySource` содержит единственный
метод `read_snapshots()`.

В этом слое отсутствуют методы создания/остановки кампаний, изменения bid/budget,
пополнения баланса или любой иной операции, способной изменить расход.

Каждая рекомендация помечена:

- `proposed_only=true`;
- `requires_owner_approval_for_mutation=true`.

Сам отчёт содержит `mutation_capability=false`.

## Input

Пока реальные Telegram Ads credentials/API намеренно не подключаются. Аналитик принимает
offline JSON snapshot:

```json
{
  "campaigns": [
    {
      "campaign_id": "example-1",
      "name": "Belgrade launch",
      "status": "active",
      "currency": "EUR",
      "captured_at": "2026-10-21T15:00:00Z",
      "spend": "15.50",
      "impressions": 2000,
      "clicks": 100,
      "joins": 8
    }
  ]
}
```

`captured_at` обязан содержать timezone. Отрицательные значения, `clicks > impressions`,
`joins > clicks`, повторяющиеся campaign ID и смешивание валют в одном агрегированном
отчёте блокируются.

## Metrics

Для каждой кампании считаются наблюдаемые:

- CTR;
- join rate from click;
- CPM;
- cost per click;
- cost per join.

Агрегат хранит spend/impressions/clicks/joins и общий observed cost-per-join.

## Recommendations

Политика задаётся до анализа:

- target cost per join;
- minimum impressions;
- minimum joins для efficiency-вывода;
- high-cost multiplier.

Результат ограничен предложениями:

- `observe` — мало данных или кампания не активна;
- `investigate` — нет delivery либо observed cost-per-join существенно выше цели;
- `maintain` — observed cost-per-join около цели;
- `consider_scale` — observed cost-per-join не выше цели и выборка прошла минимумы.

Даже `consider_scale` не является командой увеличить бюджет. Это только кандидат на решение
владельца или будущего bounded executor.

## CLI

```bash
relocant-ads-analyze \
  --input ads-snapshot.json \
  --output ads-report.json \
  --target-cost-per-join 3.00
```

CLI работает только с локальным файлом, не принимает Telegram credentials и создаёт новый
output-файл без перезаписи существующего.

## Future executor boundary

Если позже появится настоящий Telegram Ads adapter, read-only source должен жить отдельно от
executor. Executor не должен импортироваться в newsroom/analyst process и обязан иметь:

1. отдельные credentials;
2. budget guard;
3. audit trail;
4. idempotency;
5. `OWNER_APPROVAL` для bid/budget/spend, пока владелец явно не определит bounded policy.

Текущий PR executor не создаёт и реальные кампании/бюджеты не меняет.
