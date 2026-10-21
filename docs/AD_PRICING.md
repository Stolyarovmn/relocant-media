# Политика цен для рекламы в канале

Pricing отделён от suggested-post review и от Telegram Ads. Он оценивает только внутренний
**price candidate** для уже проверенной рекламной заявки. Модуль не отправляет коммерческое
предложение, не публикует пост, не списывает деньги и не меняет Telegram Ads bid/budget/spend.

## Текущее состояние

`cities/belgrade/ad-pricing.yaml` намеренно содержит:

```yaml
configured: false
currency: null
packages: {}
approval:
  mode: owner_always
```

В репозитории нет утверждённых владельцем ставок, валюты или допустимых ценовых диапазонов.
Поэтому система **не подставляет никаких значений по умолчанию**. Пока `configured: false`,
результат всегда `owner_configuration_required`, а `currency`, `base_price` и `candidate_price`
в результате равны `null`, даже если вызывающий код передал произвольную сумму.

## Явная будущая настройка владельцем

Чтобы включить pricing, владелец должен самостоятельно задать все денежные параметры. Пример
ниже показывает только форму конфигурации, а не рекомендуемые ставки:

```yaml
version: owner-defined-v1
configured: true
currency: <THREE_LETTER_CODE>
packages:
  <package_id>:
    base_price: '<EXPLICIT_POSITIVE_DECIMAL>'
    minimum_without_owner: '<EXPLICIT_POSITIVE_DECIMAL>'
    maximum_without_owner: '<EXPLICIT_POSITIVE_DECIMAL>'
approval:
  mode: owner_always  # или bounded
```

Для каждого пакета требуется `minimum_without_owner <= base_price <= maximum_without_owner`.
Частично заполненная конфигурация отклоняется, неизвестные поля отклоняются, нулевые/отрицательные
и бесконечные значения запрещены. Policy hash SHA256 входит в результат оценки, чтобы решение
можно было связать с точной версией rate card.

Режимы согласования:

- `owner_always` — любая цена требует владельца, даже если находится внутри заданного диапазона;
- `bounded` — price candidate внутри явно заданного владельцем диапазона может быть помечен как
  не требующий дополнительного ценового согласования. Это **не разрешает отправку оффера или
  публикацию**; таких capability в модуле нет. Цена вне диапазона снова требует владельца.

## Зависимость от suggested-post review

Даже после включения rate card цена может получить `price_candidate_ready` только если:

1. существует review текущей версии `AdRequest.payload`;
2. его hash совпадает с текущим payload;
3. effective outcome равен `accept_candidate` или `counter_offer_candidate`.

Иначе результат — `suggested_post_review_required`, `suggested_post_review_stale` или
`suggested_post_not_priceable`, и `owner_approval_required=true`.

Таким образом цена не может обойти scam/restricted-category/audience/claim safety floor из
[SUGGESTED_POST_REVIEW.md](SUGGESTED_POST_REVIEW.md).

## Read-only CLI

```powershell
relocant-ad-price `
  --policy cities/belgrade/ad-pricing.yaml `
  --ad-request-id 15 `
  --package-id <owner-configured-package> `
  --candidate-price <explicit-price>
```

При текущем `configured:false` команда не раскрывает и не создаёт денежные значения. После
будущей настройки она только возвращает evaluation JSON. Она не имеет `send`, `publish`,
`charge`, `set_bid`, `set_budget` или `spend` действий.

## Границы

- Никаких выдуманных ставок, валют или скидок.
- Никакого автоматического коммерческого сообщения рекламодателю.
- Никакого Telegram publish/edit/delete.
- Никакого Telegram Ads executor или money mutation.
- Изменившийся suggested-post review должен быть выполнен заново до pricing.
- Реальные rate card и bounds остаются владельческими данными и должны быть заданы явно.
