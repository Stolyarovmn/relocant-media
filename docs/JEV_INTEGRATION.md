# TypeSafe Jev — отключаемый shadow-наблюдатель

Jev не меняет очередь, candidate selection, Gemini rank/claims/verify/draft,
deterministic dedup или publication policy. Единственный выключатель —
`JEV_ENABLED`. При выключенном Jev адаптер не читает `TYPESAFE_API_KEY`,
не создаёт HTTP-клиент и не обращается к TypeSafe.

## Действующая редакция: systemd на сервере

Установленный запуск через `relocant-editorial.service -> run-cycle.sh`
подтверждён владельцем 24 октября 2026 года. Доказательства и ограничения —
в [RUNTIME_VERIFICATION.md](RUNTIME_VERIFICATION.md).
GitHub Actions проверяет код и отдельно API-ключ, но не выполняет этот service.

Jev hooks уже находятся в используемом программном пути:

- `primary_cycle.run_cycle()` после детерминированного выбора вызывает
  `rank_shadow()` и пишет `manifest.json -> jev_shadow`;
- `deploy/server/run-cycle.sh` после успешного цикла запускает
  `newsroom_status --database --city belgrade --limit 100 --shadow-duplicates`
  под тем же worker lock и сохраняет `jev-shadow-duplicates.json`.

Проверить установленную версию и фактический env-файл, не раскрывая секреты:

```bash
git -C /opt/relocant-media/app rev-parse HEAD
systemctl show relocant-editorial.service -p EnvironmentFiles
```

Unit из репозитория использует `/etc/relocant-media.env`. Если live unit показывает
другой файл, использовать именно фактический EnvironmentFile. В нём владелец
задаёт локально:

```dotenv
JEV_ENABLED=true
TYPESAFE_API_KEY=<ключ TypeSafe>
```

Файл должен оставаться root-owned с правами 0600. Не передавать ключ в Git, PR,
issue, чат, команды с ключом в аргументах, логи или артефакты.
Для полного выключения достаточно `JEV_ENABLED=false`.

Repository Secret доступен Actions workflow, который явно передаёт его шагу;
он не становится переменной отдельного systemd-процесса. Серверный ключ не
создан и не проверен этим документационным изменением.

Не запускать и не перезапускать editorial service вручную ради проверки Jev:
обычный service может публиковать. После настройки дождаться следующего штатного
timer и проверить его `manifest.json -> jev_shadow` и duplicate report в
`/var/lib/relocant-media/runs/<run>/`. Обычная редакционная политика сохраняется.

`ok` подтверждает успешный HTTP-вызов и разбор ответа; `missing_api_key` означает,
что ключ недоступен процессу, `unavailable` — сбой API/сети/контракта.
`no_public_official_candidates` не подтверждает вызов API: подходящих входов нет.
При выключенном Jev rank-shadow поле может отсутствовать, а duplicate report
не создаётся. Отсутствие поля само по себе также возможно на старом checkout
или незавершённом проходе, поэтому нужно проверять SHA и отчёт целиком.

## Отдельная проверка API в GitHub Actions

Для [jev-smoke.yml](../.github/workflows/jev-smoke.yml) остаются:

- Repository variable `JEV_ENABLED=true` (либо `false`);
- Repository secret `TYPESAFE_API_KEY`.

Smoke запускается вручную или при изменении workflow/адаптера на main.
Он использует общий HTTP-адаптер и фиксированный публичный пример из
[официальной документации](https://docs.typesafe.ai/api/system-one).
Один включённый запуск делает не более одного запроса, без retry.
При выключенном Jev secret не передаётся шагу и запрос не выполняется.

Проверка не требует БД, Gemini или Telegram credentials и не читает newsroom state.
`TELEGRAM_RUNTIME_ENABLED`, `TELEGRAM_MTPROTO_ENABLED` и
`EDITORIAL_SEND_ENABLED` принудительно выключены.
Artifact `jev-smoke-<run_id>-<attempt>/manifest.json` содержит:

```text
kind = jev_api_smoke
production_benchmark = false
newsroom_state_accessed = false
telegram_transport = none
jev_shadow.status/model/duration_ms/input_tokens/output_tokens/probabilities
```

Успех smoke подтверждает ключ только в Actions; это не benchmark качества новостей
и не доказательство наличия ключа в редакционном процессе.

## API и передаваемые данные

Контракт адаптера: `POST https://api.typesafe.ai/v1/systemone`, Bearer auth,
`jev-latest`, `noul`-вопросы, вероятности 0..1 и `usage.input_tokens/output_tokens`.

Rank-shadow передаёт до пяти title/body excerpts материалов с official ownership;
duplicate-shadow — до восьми пар официальных материалов, отобранных сервисом.
URL и private/discovery-only материалы не должны передаваться. Публичные excerpts
ограничены 600 символами на поле; timeout — 3 секунды, без retry.

## Границы и benchmark

`mode=shadow_only`, `decision_effect=none`: автоматического удаления, merge событий
или блокировки кандидатов нет. Ошибка Jev не отменяет редакционный результат.
Jev не является evidence и не заменяет official-source, freshness, moderation,
high-risk review, recheck, городскую маршрутизацию или Gemini.

После фактического включения оценить реальные rank/dedup signals, latency,
input/output tokens, стоимость по действующему тарифу, agreement и ошибки на
размеченных русских/сербских материалах. Сохранение выбора покрыто regression-тестами,
но качество и экономию нельзя объявлять доказанными без измерений.
