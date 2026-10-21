# Editorial experiment registry

Эксперименты нужны только для измеримого развития подачи контента. Они не могут
ослаблять fact-check, source policy, audience relevance, high-risk category guards или
доставочную идемпотентность.

## Registry

`editorial_experiment` заранее фиксирует:

- `experiment_id` (`id` в БД) и город;
- статус: `draft`, `running`, `paused`, `completed`, `rolled_back`;
- безопасный scope: только `presentation` или `content_style`;
- `control_rule_version` и `treatment_rule_version`;
- начало и необязательный конец eligibility window;
- заранее выбранную метрику успеха;
- горизонт измерения;
- минимальное число публикаций на arm;
- `noise_floor` — минимальную наблюдаемую разницу, которую вообще имеет смысл обсуждать;
- ограничение доли просмотров одного поста, чтобы единичный выброс не определял вывод;
- явный rollback marker и версию правил для отката.

В v1 поддерживается только `engagement_per_view_observed` на сопоставимом
`horizon_hours`. Это намеренно узкий контракт: новые метрики добавляются кодом и тестами,
а не произвольной строкой от модели.

## Assignment

`choose_assignment()` детерминированно выбирает control/treatment по SHA256 от
`experiment_id + raw_item_id`. Один и тот же материал всегда получает тот же arm.
Assignment возвращается только для `running`-эксперимента внутри eligibility window.

Выбор arm сам по себе ничего не публикует и не меняет правила. После фактической
подготовки `persist_assignment()` разрешает сохранить provenance только если:

1. пост действительно существует;
2. применённая `editorial_rule_version` совпадает с версией выбранного arm;
3. сохранённая в Post версия правил совпадает с заявленной применённой версией;
4. прежний assignment не пытаются переписать.

Поэтому нельзя задним числом объявить обычный пост treatment-публикацией.
`experiment_assignment` связывает experiment, raw item, post, arm и фактически
применённую rule version. Один post не участвует в нескольких экспериментах одновременно,
чтобы не смешивать эффекты.

## Evaluation

`evaluate_experiment()` — read-only описательная оценка. Для каждого arm она берёт
последний снимок точно на заранее заданном горизонте, исключает посты без наблюдаемых
просмотров и считает средний engagement-per-view по публикациям.

Результат может быть только наблюдением:

- `insufficient_samples`;
- `concentration_guard`;
- `within_noise_floor`;
- `treatment_higher_observed`;
- `control_higher_observed`.

Поле `automatic_winner` всегда `null`. Даже при достаточной выборке система не меняет
редакционные правила, prompt, source policy или production configuration автоматически.
LLM не участвует в вычислении результата и не может объявить победителя.

`decision_eligible=true` означает лишь, что выполнены технические минимумы выборки и
концентрации. Это не статистическое доказательство причинности и не команда на rollout.

## Rollback

`mark_rollback()` вызывается только явным управляющим действием. Он может указать лишь
control/treatment rule version данного эксперимента, переводит registry в `rolled_back` и
сохраняет время. Evaluation никогда не вызывает rollback самостоятельно.

## SQLite

Registry добавляет SQLite schema v2. Обновление существующей v1 базы выполняется только
явным `initialize_sqlite`: перед изменением создаётся `*.before-v2.backup`, затем создаются
две новые таблицы и только после integrity/FK/schema проверки выставляется user_version=2.
Существующие raw items, post IDs, editorial runs и Telegram receipts не пересоздаются.

## Что запрещено

- экспериментировать с factual/source-risk/safety guards;
- менять правила на основании единичного удачного поста;
- считать отсутствующие просмотры нулями;
- сравнивать разные горизонты как один эксперимент;
- разрешать LLM выбирать победителя;
- автоматически включать treatment или rollback по результату evaluation.
