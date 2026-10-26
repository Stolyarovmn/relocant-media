# Восстановление серверного deploy при SQLite v7 → v8

Это временный существующий серверный контур. Он сохраняет ту же каноническую SQLite,
Telegram IDs, offsets и `sent/sending/uncertain`; новую очередь и новые отправки
инженерная операция не создаёт. Merge и CI сами по себе не подтверждают восстановление
сервера. Применять только к серверу, где Actions-писатель ещё не активирован.

## Что делает новый deploy

После успешного CI на точном текущем `main` скрипт останавливает только редакционные
таймеры, дожидается `worker.lock` и захватывает `intake-consumer.lock`. Занятый intake
приводит к безопасному переносу deploy; активные процессы не убиваются. Оба файловых
дескриптора удерживаются до окончания установки или rollback. Maintenance проверяет,
что их inode принадлежат lock-файлам рядом именно с канонической SQLite.

`server_database_maintenance upgrade` требует существующий локальный `DATABASE_URL`
без query-параметров. Он допускает только полную схему v7 либо уже совместимую v8.
Нельзя автоматически инициализировать пустую БД или обновлять неизвестные/legacy версии.
`verify_sqlite_schema` остаётся проверкой без мутаций.

Для v7 до изменений создаётся уникальная SQLite backup-копия и квитанция рядом
с `worker.lock` с режимом `0600`, принадлежащие пользователю deploy (обычно root).
SQLite backup включает committed WAL; выполняются integrity/FK, hash/readback и fsync.
В одной транзакции добавляется только `raw_item.published_at_precision DEFAULT 'unknown'`
и меняется `user_version=8`: точность исторических дат не придумывается. Проверяется
сохранность всех прежних строк и полей, включая квитанции Telegram. Права и владелец
живого файла не меняются. Если read-only reader удерживает WAL/SHM после выхода root,
владелец и режим оставшихся sidecars приводятся к исходному файлу БД; живой WAL не
удаляется. Бэкапы после успешного deploy не удаляются автоматически.

Ошибка последующей установки/main-sync или INT/TERM вызывает rollback под теми же
lock-файлами: сначала проверенная БД v7, затем прежний checkout/packages и точные прежние
установленные systemd-файлы. Если после миграции БД менялась, rollback её не перезапишет.
Если восстановление не подтверждено, редакционные таймеры останутся остановленными.
Ошибка запуска таймеров возвращает ненулевой статус и требует отдельного разбора;
она не откатывает уже проверенную БД и код.

## Однократный bootstrap старого root-owned deploy-скрипта

Старый установленный `/usr/local/libexec/relocant-media/deploy-green-main.sh` может
откатить checkout после проверки v7 **до установки новой версии самого себя**.
Обычный запуск этого старого service проблему не исправляет.

После merge взять полный SHA с подтверждённым push-CI `main`, включающий recovery-код.
Подставить его в `recovery_sha` ниже. Команда предназначена для терминала существующего
сервера с root-доступом; секреты не передаются в чат или аргументах. Она не запускает
редакционный service вручную. Стандартные пути должны совпадать с реальными units;
если deployment ранее изменял их, использовать фактическую конфигурацию, а не менять её.

```sh
sudo /bin/sh <<'SH'
set -eu
umask 022
recovery_sha='REPLACE_WITH_VERIFIED_FULL_MAIN_SHA'
case "$recovery_sha" in *[!0-9a-f]*) echo 'full verified SHA required' >&2; exit 78;; esac
[ "${#recovery_sha}" -eq 40 ]
app=/opt/relocant-media/app
remote=https://github.com/Stolyarovmn/relocant-media.git
repo=Stolyarovmn/relocant-media
# Stop the old watcher timer; do not kill an already-running deploy.
systemctl stop relocant-deploy.timer
if systemctl is-active --quiet relocant-deploy.service; then
  echo 'Deploy is active: wait for completion before repeating recovery.' >&2
  exit 75
fi
[ -z "$(git -c safe.directory="$app" -C "$app" status --porcelain)" ]
git -c safe.directory="$app" -C "$app" fetch --no-tags "$remote" refs/heads/main
[ "$(git -c safe.directory="$app" -C "$app" rev-parse FETCH_HEAD)" = "$recovery_sha" ]
curl -fsSL -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$repo/actions/runs?head_sha=$recovery_sha&event=push&per_page=100" |
  python3 -c 'import json,sys
sha=sys.argv[1]
runs=json.load(sys.stdin)["workflow_runs"]
ci=[r for r in runs if r.get("head_sha")==sha and r.get("head_branch")=="main"
    and r.get("event")=="push" and r.get("path")==".github/workflows/ci.yml"]
assert ci and ci[0]["status"]=="completed" and ci[0]["conclusion"]=="success"
' "$recovery_sha"
recovery_dir=$(mktemp -d /run/relocant-recovery.XXXXXX)
trap 'rm -rf "$recovery_dir"' EXIT
git -c safe.directory="$app" -C "$app" show \
  "$recovery_sha:deploy/server/deploy-green-main.sh" > "$recovery_dir/deploy.sh"
chmod 0700 "$recovery_dir/deploy.sh"
RELOCANT_EXPECTED_TARGET_SHA="$recovery_sha" RELOCANT_REPAIR_DATABASE=true \
  /bin/sh "$recovery_dir/deploy.sh"
systemctl is-active relocant-editorial.timer relocant-intake.timer relocant-deploy.timer
SH
```

Bootstrap использует `umask 022`: git и editable-установка Python должны оставаться
читаемыми служебному пользователю `relocant`. Маска `077` создаёт `.pth` с режимом
`0600` и делает установленный пакет недоступным коллектору. Это не открывает секреты:
временный каталог создаётся через `mktemp -d`, скрипт имеет `0700`, а maintenance
явно создаёт резервную базу и квитанцию с `0600`. Права существующего env и ключей
не меняются.

Повторная проверка pinned SHA внутри deploy исключает выполнение скрипта со старого
SHA поверх нового, продвинувшегося `main`: операция отложится до остановки таймеров.
`RELOCANT_REPAIR_DATABASE=true` проводит полный защищённый путь даже когда checkout
уже совпадает с `main` (например, после ручного pull). Обычный watcher сохраняет дешёвый
no-op, когда код и control plane уже установлены. При ошибке предварительной проверки
старый deploy timer остаётся остановленным: сначала разобрать ошибку, затем повторить
команду с актуальным проверенным SHA. Не включать редакцию поверх неясного rollback.

## Что подтвердить после команды

Проверить journal deploy, SHA checkout, активность трёх timers и следующий естественный
редакционный прогон: время начала/завершения, ошибки, сохранённые message IDs и дату
опубликованного Pages JSON. Не запускать production-отправки ради теста.

SIGKILL, потеря питания/runner и ошибки диска не дают гарантии исполнения shell traps.
Квитанция сохраняет до commit ожидаемые fingerprints v7 и v8; повреждённый/изменённый
файл автоматически не восстанавливается. После такого сбоя требуется проверить journal,
квитанцию и текущий schema/checkpoint перед возобновлением writers. Не удалять SQLite,
не сбрасывать offsets, `uncertain` или `busy` и не восстанавливать старую копию поверх
новых Telegram-эффектов.
