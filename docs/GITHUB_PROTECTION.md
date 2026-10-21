# Защита ветки `main`

Состояние на 21.10.2026: GitHub branch object сообщает `protected=false`, required
status checks выключены. Подключённый к ChatGPT GitHub App умеет читать ветку и
работать с файлами/PR, но административный endpoint
`GET /repos/Stolyarovmn/relocant-media/branches/main/protection` отвечает
`403 Resource not accessible by integration`. Поэтому эта настройка не должна
считаться включённой, пока владелец не применит её через GitHub UI или credential
с repository administration permission.

## Требуемая политика

Для `main` включить branch protection или ruleset со следующими ограничениями:

- изменения только через pull request;
- обязательный успешный check `ci` перед merge;
- блокировать merge при устаревшем head, если GitHub позволяет требовать актуальную ветку;
- запретить force-push;
- запретить удаление `main`;
- не разрешать обход обязательных checks автоматическому агенту;
- оставить возможность владельцу аварийного администрирования только как осознанный bypass,
  а не штатный путь разработки.

Требование review от другого человека намеренно не делается обязательным: владелец
21.10.2026 дал standing permission агенту создавать и сливать PR после зелёного CI.
Это разрешение не позволяет обходить CI или остальные safety-boundaries проекта.

## Проверка после включения

1. Открыть `Settings → Rules → Rulesets` или `Settings → Branches` и убедиться, что правило
   применяется к `main`.
2. Проверить, что required check соответствует фактическому workflow `.github/workflows/ci.yml`.
3. Проверить, что force-push и deletion запрещены.
4. После настройки запрос `GET /branches/main` должен показывать `protected=true`.
5. Попытка merge PR с красным/незавершённым `ci` должна быть заблокирована GitHub.

До выполнения этих шагов контракт `CLAUDE.md` остаётся обязательным программным правилом,
но GitHub технически не гарантирует его enforcement.
