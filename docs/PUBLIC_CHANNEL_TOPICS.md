# Публичные Telegram-каналы как указатели тем

Проверка 22 октября 2026 года: обычный HTTPS GET публичных `/s/` страниц,
без аккаунта, подписок и MTProto. Подписчики — округлённые видимые счётчики
конкретного снимка; они не доказывают качество, независимость или достоверность
информации.

| Канал | Подписчики в снимке | Назначение |
|---|---:|---|
| `@Serbia` | 44.4K | Широкие новости Сербии на русском |
| `@SerbiaInMyMind` | 31.1K | Сербия, релокация и локальные темы |
| `@serbia_news` | 24.7K | Новости Сербии на русском |
| `@serbnews` | 21.2K | Новости Сербии на русском |
| `@novostiserbii` | 17.2K | Новости Сербии на русском |
| `@beogradske_vesti` | 15K | Практические городские новости Белграда |
| `@ruserbia` | 13K | Новости и локальные темы Сербии |
| `@sta_imas_beograd` | 12.5K | События Белграда |
| `@relocate_serbia` | 11.7K | Релокация и локальные темы |
| `@volna_srbjia` | 10.1K | Релокация и локальные новости |
| `@vstrechi_v_belgrade` | 9.62K | События Белграда |
| `@afisha_rs` | 4.09K | Афиша; встречаются реклама и устаревшие анонсы |

Это discovery-набор, а не рейтинг доверия. Русскоязычная популярность не заменяет
проверку фактов у перевозчика, организатора, ведомства или другого допустимого
первичного источника. Канал не становится publication evidence только из-за размера
аудитории или повторения новости несколькими каналами.

`collectors.telegram_topics.DEFAULT_CHANNELS` задаёт явный список. Сборщик возвращает
`TopicLead`, а не публикационный `RawItem`. До извлечения текста отбрасываются посты
старше 24 часов, посты без корректной даты и даты из будущего. Старый текст не
передаётся дальше. Отсутствующие просмотры остаются `None`, округлённые помечены
`approximate`; реакции, пересылки и комментарии не выдумываются. HTML получен целиком,
но не сохраняется сборщиком.

Внешние ссылки сохраняются только как непроверенные HTTPS-указатели: сборщик их не
открывает. Вызывающая сторона обязана проверить разрешённые источники и извлечь
оригинал отдельно. Пост без пригодного первоисточника может дать короткую отметку
`needs_primary_source`, но не основание для публикации и не повод расходовать ИИ на
пересказ неподтверждённого текста. Перепечатки нескольких каналов не считаются
независимыми подтверждениями.

При изменении HTML, закрытом просмотре, перенаправлении или HTTP-ошибке сборщик выдаёт
`PreviewError`. Публичный просмотр может быть неполным; отсутствие свежего поста здесь
не доказывает его отсутствия в Telegram.

## Runtime coverage observations

The Belgrade runtime merges its city-scoped defaults with Telegram entries marked
`runtime_public_preview` in `docs/messenger-discovery.json`, deduplicating usernames
case-insensitively. Catalog-only jobs channels remain excluded; another city's registry
entries do not enable that city's runtime.

`messenger-coverage.json` records each attempted channel separately from resolved topics:
`dated_messages`, `stale_snapshot`, `freshness_unknown`, `future_dates`, `landing_only`,
`unsupported_preview`, or `access_error`. Empty results are not proof of an empty news
interval. Clock-only or missing dates trigger at most twenty public same-post permalink
reads per channel (five-second request timeout); unresolved dates are never guessed and
those posts cannot become fresh leads. A dated old preview means stale observed messages,
not proof that the upstream response was served from cache.

Coverage remains `partial`: a public preview does not prove complete pagination of the
requested interval. A submissions-only run records `not_checked` instead of claiming an
ordinary messenger sweep. Viber/WhatsApp entries without a supported public adapter are
listed as `blocked_no_message_access`; visiting a publisher website does not count as
reading its messenger messages. Discovery texts and private payloads are not added to this
coverage artifact, and messenger leads still require independent official evidence.
