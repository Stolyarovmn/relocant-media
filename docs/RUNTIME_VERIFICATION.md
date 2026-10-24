# Проверка фактического редакционного runtime

## Подтверждённый запуск на сервере

24 октября 2026 года владелец предоставил вывод `systemctl` с рабочего сервера:

- установлен `/etc/systemd/system/relocant-editorial.service`;
- `WorkingDirectory=/opt/relocant-media/app`;
- `ExecStart=/opt/relocant-media/app/deploy/server/run-cycle.sh`;
- последний запуск: 09:00:38–09:02:32 UTC, `Result=success`;
- активны `relocant-editorial.timer` и отдельный пятиминутный
  `relocant-intake.timer`.

Это подтверждение установленного server runtime, а не вывод по наличию unit-файлов
в Git. `Result=success` означает успешное завершение сервиса, но само по себе не
доказывает публикацию или качество материала.

Код проверен на `main` `4f77222af46fc3606ca921ca2773fd0dcf742156`.
Точный SHA установленного серверного checkout указанным выводом не подтверждён.

## Подготовка и настоящая доставка — разные этапы

Штатный wrapper вызывает
`server_runtime -> server_cycle.main -> primary_cycle.run_cycle`.
Общий цикл выбирает кандидатов, вызывает optional Jev shadow и подготавливает
проверенные пакеты. Его `telegram_transport="fake"` описывает только этот этап.

Затем `server_cycle.main` отдельно вызывает `publish_pending()`, используя
`ChannelPublisher` и настоящий Bot API при `EDITORIAL_SEND_ENABLED=true`.
Успешная доставка сохраняет `Post.telegram_message_id`, `published_at` и
`EditorialRun.delivery_status="sent"`; внешний отчёт содержит
`channel_transport` и `sent_message_ids`.

Поэтому внутреннее поле `fake` не доказывает отсутствие production delivery.
Свежий согласованный снимок рабочей SQLite от 07:41:36 UTC содержит квитанции:
message 27 в 07:15:53 UTC и message 28 в 07:35:55 UTC. Для message 28 материал
`raw_item_id=398` связан с подготовительным scheduler run 30, который завершился
в 07:35:54 UTC, и facts/utility reviews `server-facts`/`server-utility`.
Снимок проверен в read-only режиме: quick_check=ok и SHA256 совпал с manifest.
Эти данные подтверждают сохранённый результат доставки, но не являются live-чтением
Telegram или отдельным подтверждением активной настройки Jev.

Проверяемые точки:
[wrapper](../deploy/server/run-cycle.sh),
[server runtime](../src/relocant_media/ops/server_runtime.py),
[подготовка и доставка](../src/relocant_media/ops/server_cycle.py),
[фиксация квитанций](../src/relocant_media/services/channel_delivery.py).

## Что выполнял GitHub Actions

В предыдущей проверке истории Actions (1476 доступных runs, 15 страниц) у
`editorial-primary` было 17 запусков: 16 `skipped`, 1 `failure`, 0 `success`.
Это исторический срез, не постоянно обновляемый счётчик.

[Manual run 40064995861](https://github.com/Stolyarovmn/relocant-media/actions/runs/40064995861)
остановился на пустом `DATABASE_URL` до обработки. Удалённый в
[PR #143](https://github.com/Stolyarovmn/relocant-media/pull/143)
workflow использовал staging CLI, не получал Telegram token и не запускал
полный `server_runtime`. Он не объясняет действующую публикацию.

Текущие workflows — CI, source-health, daily-analysis placeholder и
[jev-smoke](../.github/workflows/jev-smoke.yml). Отдельный Jev smoke проверяет
Actions Secret на фиксированном публичном примере; он не подключает этот secret
к окружению systemd и не является benchmark редакции.

## Jev и оставшиеся эксплуатационные проверки

Jev уже вызывается в общем `run_cycle()` после детерминированного отбора, а
wrapper содержит optional duplicate-shadow после успешного server cycle.
Для реальных сигналов настройки должны быть доступны процессу редакции на сервере:
см. [JEV_INTEGRATION.md](JEV_INTEGRATION.md).

До утверждения, что Jev включён в production, проверить:

1. точный установленный SHA: `git -C /opt/relocant-media/app rev-parse HEAD`;
2. фактический путь EnvironmentFile:
   `systemctl show relocant-editorial.service -p EnvironmentFiles`;
3. `manifest.json -> jev_shadow` и `jev-shadow-duplicates.json` штатного прохода.

Не читать/публиковать содержимое env-файла или ключей в диагностике.
Не создавать вторую БД, не заменять SQLite PostgreSQL и не включать второго писателя.
Наличие hooks, зелёный CI и успешный сервис не доказывают HTTP-вызов TypeSafe:
его подтверждает только telemetry конкретного shadow-вызова.
