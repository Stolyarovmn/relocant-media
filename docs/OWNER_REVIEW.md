# Owner review (решения владельца)

Политика и другие high-risk категории не публикуются автономно. После обычной проверки claims/evidence сервер оставляет материал в `human_review`. Пятиминутный owner-review worker затем:

1. повторно связывает материал с неизменным event evidence bundle;
2. выполняет две независимые проверки (`facts` и `utility`) по живым первоисточникам;
3. отправляет точный проверенный черновик владельцу в личный Telegram-чат;
4. добавляет кнопки `✅ В редакцию` и `❌ Отклонить`;
5. принимает reply владельца как комментарий к точной версии черновика;
6. применяет комментарий только в пределах уже подтверждённых claims, удаляет старые review и выполняет обе проверки заново;
7. присылает новую версию на согласование;
8. повторяет цикл до публикации или отклонения.

## Что попадает владельцу

Только материалы, по которым редакция сама не смогла решить, и только свежие и с готовым черновиком. Причины задаёт `OWNER_DECISION_REASONS`: `category_not_auto_publishable`, `package_requires_review`, `source_auto_publish_disabled`, `policy_requires_human_review`. Категория может быть любой, не только high-risk.

Не попадают: `publication_date_unknown`, `source_changed_requires_review`, `blocked`, `skipped`, устаревшие материалы. Это вопросы происхождения, даты и фактов, их решение владельца не заменяет. «В редакцию» означает согласование темы и сути: две независимые проверки, свежесть, live-проверка первоисточника, официальный источник и политика аудитории выполняются снова перед отправкой и могут остановить публикацию.

Материалы проверенных СМИ (`ownership: media`) идут тем же путём, но только вне высокорисковых категорий и только с собственным пересказом; см. [MEDIA_SOURCES.md](MEDIA_SOURCES.md).

## Защитные границы

- owner approval дополняет, а не заменяет evidence/provenance и независимые reviews;
- discovery/media text не становится publication evidence;
- публикация требует официального primary evidence в frozen event bundle;
- кнопка связана с точными `package_sha256`, `draft_sha256`, review message ID и Telegram owner ID;
- старая кнопка после изменения версии недействительна;
- owner comment не может добавлять неподтверждённые факты, ссылки, числа или claims;
- перед публикацией primary evidence проверяется ещё раз live;
- обычный `deliver_reviewed` остаётся закрыт для high-risk; owner-approved публикация использует отдельный at-most-once путь;
- Telegram send с неизвестным результатом переводится в `uncertain` и автоматически не повторяется;
- отказ владельца переводит публикацию в финальное `cancelled/rejected` состояние;
- owner-review rows имеют отдельный `Submission.kind=owner_editorial_review` и не попадают в обычную очередь пользовательских submissions.

## Production configuration

Owner review использует тот же production Bot API token и существующий `relocant-intake.timer`, который запускается каждые пять минут. Отдельный webhook или второй polling consumer не создаётся.

В `/etc/relocant-media.env` требуются:

```dotenv
EDITORIAL_OWNER_REVIEW_ENABLED=true
EDITORIAL_OWNER_USER_ID=<telegram-user-id-владельца>
EDITORIAL_OWNER_CHAT_ID=<private-chat-id-с-ботом>
```

Фактическая отправка в публичный канал по кнопке дополнительно требует уже существующего:

```dotenv
EDITORIAL_SEND_ENABLED=true
```

`EDITORIAL_PRIVATE_INTAKE_ENABLED` остаётся независимым: owner review может работать даже если приём обычных личных tips отключён.

Production polling по-прежнему fail-closed при обнаружении активного webhook, чтобы два consumer не подтверждали одни и те же Telegram updates.

## Operational notes

`relocant-intake.service` сначала сохраняет Telegram update и owner decision в одной транзакции с `TelegramUpdateReceipt`, затем пятиминутный owner worker обрабатывает решение. Поэтому restart после принятой кнопки не теряет решение и не превращает его в повторную публикацию.

Для комментария владелец использует обычную функцию Telegram **«Ответить»** на последнее сообщение review. Ответ на старую/уже обработанную версию не меняет текущий материал.
