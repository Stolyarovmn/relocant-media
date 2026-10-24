# Проверка runtime перед подключением Jev

Проверено 24 октября 2026 года на `main` `7e2afd4ef6bc2958ad95cc67ccb07d0974a9c19b`.
Это наблюдения о доступном коде и GitHub, а не утверждение о состоянии удалённого сервера.

## GitHub Actions

Проверены все 1476 доступных workflow runs через
[Actions API](https://api.github.com/repos/Stolyarovmn/relocant-media/actions/runs?per_page=100)
(15 страниц). В них `editorial-primary` имеет 17 запусков: 16 `skipped`, 1 `failure`,
0 `success`. Это не исключает удалённые runs или запуск из другого repository/сервиса.

- [Scheduled run 39988874983](https://github.com/Stolyarovmn/relocant-media/actions/runs/39988874983)
  завершился `skipped`.
- [Manual run 40064385537](https://github.com/Stolyarovmn/relocant-media/actions/runs/40064385537)
  также `skipped`.
- [Manual run 40064995861](https://github.com/Stolyarovmn/relocant-media/actions/runs/40064995861),
  job `120786251253`: Jev configuration validation успешна, но `DATABASE_URL` пустой,
  CLI останавливается до обработки. Это подтверждает только передачу непустого ключа,
  а не успешный TypeSafe API call.

[Workflow до удаления](https://github.com/Stolyarovmn/relocant-media/blob/e9204e8455b78ad35bc55a1f64b070b9a5cefb96/.github/workflows/editorial-primary.yml)
использовал `ubuntu-latest`, `APP_ENV=staging` и `primary_cycle` CLI. Он не получал
Telegram token, выключал Telegram runtime и не содержал SSH/SFTP/download/upload
канонической SQLite. CLI вызывает `_run(..., "fake")` и собирает только два базовых
источника. Поэтому этот workflow не объясняет реальные публикации канала.

[PR #143](https://github.com/Stolyarovmn/relocant-media/pull/143) удалил workflow и
добавил shadow dedup в серверный wrapper. Его
[post-merge CI 40066097133](https://github.com/Stolyarovmn/relocant-media/actions/runs/40066097133)
успешен, но CI не проверяет установленный publisher/runtime.

## Код альтернативного entrypoint

`server_runtime -> server_cycle.main -> primary_cycle.run_cycle` реализует расширенный
сбор, Gemini, независимые facts/utility reviews, live recheck и guarded delivery.
`server_cycle.main` требует существующий **локальный SQLite**. Доставка включается через
`EDITORIAL_SEND_ENABLED`; одного `TELEGRAM_RUNTIME_ENABLED=false` для этого entrypoint
недостаточно. Указанный путь можно проверить в
[server_cycle.py](../src/relocant_media/ops/server_cycle.py).

В Git есть systemd units и пути `/opt/relocant-media/app`, `/etc/relocant-media.env`,
`/var/lib/relocant-media/runs`. Их наличие не доказывает установку, активность или
последнюю успешную отправку. Доступ к серверу из текущей инженерной среды недоступен;
реальные units/processes/receipt rows этой проверкой не подтверждены.

## Что требуется установить до runtime migration

Нужно связать конкретную недавнюю Telegram-квитанцию с `post`/`editorial_run`, run report,
точным кодовым SHA и фактическим процессом/runner. Затем установить путь доступа к
канонической SQLite, общий writer lock и сохранение состояния между запусками.

Нельзя считать зелёный CI доказательством публикации; создавать пустую DB вместо
действующей; переносить production execution между Actions/systemd по названиям файлов;
одновременно включать два писателя; копировать live SQLite без корректного snapshot и
проверки отсутствия конкурирующих writers.

Отдельный `jev-smoke` безопасно проверяет уже сохранённый Actions Secret без решения
этих вопросов. Он не объявляется подключением к действующему production newsroom.
