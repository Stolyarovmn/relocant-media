# Редакция в GitHub Actions, состояние в Google Drive

Добавлен переносимый адаптер существующей редакции. Facts/utility review, evidence,
source policy, freshness, high-risk owner review и delivery guards не заменяются.
`systemd` не является зависимостью hosted runner. Telegram остаётся интерфейсом
предложки/ревью/доставки; Pages получает только прежнюю публичную проекцию.

## Текущий предел готовности

Облачные URL-кандидаты принимаются через mode=discovery в том же workflow и той же
SQLite; формат и квитанции описаны в [CLOUD_DISCOVERY.md](CLOUD_DISCOVERY.md).
Этот вход не включает runtime и не изменяет условия переключения ниже.

Наличие workflow в main не означает выполненное переключение. Расписание выключено,
пока `ACTIONS_EDITORIAL_ENABLED` не равно `true`; отправка по умолчанию выключена.
Состояние старого сервера, остановка всех писателей и авторизация Drive из Actions
должны быть проверены отдельно. Подключение Google Drive к ChatGPT не передаёт
учётные данные на GitHub runner.

Исторический снимок Drive можно показать на Pages, но `observed_at` берётся из его
проверенного манифеста. Время сборки сайта не делает старую копию актуальной. Нельзя
использовать историческую копию для рабочего переключения или восстановления busy.

## Жизненный цикл

1. Все Actions-писатели (editorial и intake) используют одну concurrency group
   `canonical-editorial-writer`, `cancel-in-progress: false`. Нет PR/push-триггеров редакции.
2. Workflow требует актуальный main и успешный push CI на точном SHA до запуска.
3. `editorial-state/state/current.json` — закрытый канонический указатель: поколение,
   checksum/размер/версия SQLite, Drive file ID, состояние ready/busy и retry budget intake.
   Код из ветки данных никогда не исполняется. Отсутствие указателя — ошибка.
4. Runner скачивает указанный файл, проверяет SHA256, размер, SQLite schema/integrity
   существующим restore. Новая пустая БД не создаётся, миграций нет.
5. До любых Telegram-операций Contents API с ожидаемым blob SHA сохраняет busy.
   Конфликт останавливает проигравший процесс до побочных эффектов. Busy не истекает.
6. Запускается прежний server_runtime или переносимый intake_batch. Внешние статусы
   sending/uncertain/sent и Telegram offsets остаются в той же SQLite. Retry budget
   intake сохраняется отдельно в указателе; он не сбрасывается между runner-ами.
7. После завершения создаётся проверенный SQLite backup. Изменённая копия загружается
   новым файлом в закрытую папку Drive; полное скачивание подтверждает SHA256. Если байты
   не изменились, прежний immutable файл используется снова. Старые копии не удаляются.
8. Готовится публичная проекция. Intake-only обновление сохраняет последний редакционный
   отчёт/модели; новый submissions-only прогон заменяет его своим наблюдением.
9. Только после подтверждения Drive CAS меняет busy → ready с новым указателем.
   При ошибке публичный JSON удаляется, чтобы не показать незакоммиченную очередь.
10. Санитизированный JSON сохраняется в newsroom-data и размещается в том же Actions
    workflow. Обычного GITHUB_TOKEN достаточно: не требуется запускать push-workflow
    от собственного push. Ошибка Pages не запускает редакцию повторно. Отдельный
    Newsroom Pages может перечитать последний снимок без редакционного прогона.

Job получает `actions: read` для проверки CI приватного репозитория; право запуска
или изменения других workflows не требуется. После обычного завершения worker и
после ошибки runtime отдельный `last_attempt` показывает outcome, режим, время и
ссылку на Actions run. Если checkpoint не подтверждён, витрина использует только
прежнюю публичную проекцию из `newsroom-data` или реестр без данных. `observed_at`
очереди и квитанции при этом не обновляются. Этот fallback не читает рабочую SQLite
и не снимает busy. При отмене всего job, потере runner или недоступности GitHub/Pages
обновление не гарантировано: сохранённый снимок продолжает стареть.

## Однократное переключение

Это эксплуатационная операция над текущим писателем, не результат merge:

- Отключить **и запретить повторное включение** старых editorial/intake services/timers
  и deploy service/timer. Deploy watcher способен снова включить редакционные таймеры.
  Не убивать активную отправку: сначала дождаться завершения процессов и освобождения lock.
- Отключить внешние ChatGPT-процессы, меняющие ту же очередь/получающие updates.
- После остановки создать финальную проверенную пару SQLite/manifest и сохранить её
  в Drive; манифест дополнить фактическим `drive_file_id`. Перенести также существующий
  `intake-batch-state.json`. Убедиться, что offsets/квитанции не отстают от Telegram.
- Установить отдельную авторизацию для runner. `DRIVE_CREDENTIALS_JSON` — секрет Actions
  с Google ADC JSON, для личного My Drive обычно `authorized_user` с OAuth refresh token.
  Допустимы ADC/WIF при корректной настройке. Авторизация должна разрешать чтение финальной
  копии и запись в выбранную закрытую папку; код запрашивает `drive.file`.
  Для новой OAuth app этот scope может не давать доступа к старым файлам: доступ к конкретным
  файлам/папке нужно предоставить этой app. Service account не получает доступ к личной папке
  автоматически; не предполагать наличие quota/shared-drive/delegation. Секреты не коммитить.
- Подготовить переменные Actions: `DRIVE_STATE_FOLDER_ID`, `GEMINI_MODEL`,
  `EDITORIAL_OWNER_USER_ID`, `EDITORIAL_OWNER_CHAT_ID`; секреты `GEMINI_API_KEY`,
  `TELEGRAM_BOT_TOKEN` и при включённом Jev `TYPESAFE_API_KEY`. Существующие production
  секреты автоматически не создаются/не изменяются.
- В доверенной среде с ADC и GitHub Contents write выполнить однократный bootstrap:

```sh
python -m relocant_media.ops.actions_bootstrap \
  --final-manifest /secure/final-manifest.json \
  --intake-state /secure/intake-batch-state.json \
  --work /secure/new-cutover-check \
  --confirm-all-former-writers-fenced
```

`GH_STATE_TOKEN` берётся из защищённого окружения. Bootstrap требует копию не старше
часа, проверяет восстановление и никогда не перезаписывает существующее состояние.
Флаг — явное подтверждение фактически выполненного fencing, а не его замена.
Bootstrap не включает расписание и не вызывает Telegram.

- Для проверки авторизации до cutover можно задать `DRIVE_PREVIEW_MANIFEST_ID` —
  точный ID исторического JSON-манифеста в Drive, содержащего `drive_database_file_id`.
  Ручной mode=preview проверит checksum/restore и покажет прежнее время наблюдения,
  не создавая каноническую очередь и не вызывая Telegram. После bootstrap переменную
  удалить: mode=preview будет читать канонический указатель.
- Запустить `Editorial Actions` вручную с mode=preview: это только чтение/проекция.
- После подтверждённого cutover установить `ACTIONS_CUTOVER_CONFIRMED=true` и
  `ACTIONS_EDITORIAL_ENABLED=true`. `ACTIONS_SEND_ENABLED`,
  `ACTIONS_PRIVATE_INTAKE_ENABLED`, `ACTIONS_PRIVATE_FEEDBACK_ENABLED`,
  `ACTIONS_OWNER_REVIEW_ENABLED` включаются явно согласно прежним настройкам.
  В инженерной проверке не включать реальную отправку.

Расписания: editorial каждые три часа в :17 UTC; intake примерно каждые пять минут.
GitHub cron не гарантирует точный момент запуска. Сохраняется ограничение нагрузки
существующего цикла; расходы Actions и рост Drive нужно наблюдать после включения.

## Сбой и восстановление

Busy после timeout/cancel/сбоя загрузки/конфликта нельзя автоматически снимать.
Следующий запуск прекращается до Telegram. Повтор запуска того же workflow также
блокируется. Любой неизвестный исход доставки остаётся неопределённым.

Не сбрасывать busy в ready на старую копию и не использовать bootstrap для recovery:
между старой копией и аварией Telegram мог принять публикации, ответы и getUpdates.
Нужно сверить отправки/DM/callbacks и подтверждённые offsets; восстановить более новое
проверенное состояние либо явно пометить неопределённые операции, сохранив audit trail.
При полной потере runner локальные данные могут быть недоступны; блокировка сохраняет
безопасность, но не обещает автоматического восстановления/непрерывной работы.

SQLite/сырые логи/private intake/credentials никогда не входят в Pages artifacts.
Actions загружает только `_site`. Не удалять старый сервер до подтверждённого переноса;
при rollback нельзя включать его старую базу параллельно новой.

## Проверенные API

- [Drive binary download](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Drive multipart upload](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [GitHub Contents SHA precondition](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)
- [GITHUB_TOKEN event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
