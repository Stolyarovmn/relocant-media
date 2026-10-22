# Belgrade source registry

Only verified sources belong here. Do not add guessed URLs.

Initial verified official sources were checked on 2026-10-18:

- City of Belgrade main/service portal;
- Secretariat for Public Transport news;
- Secretariat for Public Transport current/planned service changes;
- Tourist Organization of Belgrade events;
- Serbia eUprava foreigner-stay registration service;
- City of Belgrade health service-information feed.

The registry deliberately starts with primary sources. Commercial media sources will be added only after a separate quality/terms/duplication review.

19.10.2026 проверен официальный сайт городской общины Чукарица:
`cukarica_news` собирает главную и полные статьи с явным отказом при изменении
шаблона. Источник не разрешает публикацию и не заменяет редакционные проверки.

21.10.2026 отдельно проверены официальные коммунальные первоисточники:

- `bvk_planned_works` — JKP Beogradski vodovod i kanalizacija, плановые и срочные работы по водоснабжению с районами, улицами и временем;
- `eds_belgrade_outages_today` / `tomorrow` / `day2` / `day3` — Elektrodistribucija Srbije, официальные таблицы плановых отключений электричества для распределительного района Белграда;
- `beoelektrane_network_works` — JKP Beogradske elektrane, текущие работы теплосети по районам и улицам;
- `parking_servis_news` — JKP Parking servis Beograd, изменения парковочных зон, режима работы, гаражные работы и сервисные предупреждения.

Новые коммунальные/сервисные источники сначала включаются только как
`page_change_detection` snapshots с `auto_publish: false`. Это намеренный shadow-этап: система
сохраняет изменения официальных страниц, но не превращает whole-page snapshot в готовую новость.
Полноценный датированный article/outage parser включается только после проверки на реальных
снимках и отдельных regression fixtures. Неподтверждённый RSS/feed URL не считается допустимым
основанием для production collector.

`bvk_planned_works` прошёл следующий shadow-этап 21.10.2026: живая официальная страница
подтверждена как список нескольких независимых уведомлений с заголовком формата
`DD.MM.YYYY. ...`, после чего добавлен структурированный `bvk_planned_works` parser и regression
fixture. Каждое уведомление получает стабильный external ID и отдельную внутреннюю canonical
identity, но публичный evidence URL остаётся точным URL BVK. Страница не показывает надёжный
publication timestamp, поэтому parser намеренно оставляет `published_at` пустым и
`auto_publish: false`; дату работ нельзя подменять датой публикации.

EDS rolling day-pages (`Dan_0` ... `Dan_3`) также прошли structured shadow-этап 21.10.2026.
На живых страницах явно присутствуют дата расписания и таблица `Општина / Време / Улице`.
`eds_belgrade_outages` превращает каждую строку в отдельный raw item со стабильным ID,
сохраняет точный публичный URL day-page и отдельную внутреннюю canonical identity. Явно пустая
датированная таблица трактуется как отсутствие запланированных отключений; исчезновение даты или
ожидаемого заголовка таблицы считается ошибкой структуры. Дата расписания не выдаётся за дату
публикации: `published_at` остаётся пустым, `auto_publish: false` сохраняется до измерения качества
live extraction/freshness.

22.10.2026 добавлены ещё два проверенных vertical source в shadow-режиме:

- `srbijavoz_news` — официальный раздел новостей пассажирского оператора Srbijavoz. Он используется
  как первичный источник изменений железнодорожного движения и уведомлений, затрагивающих в том
  числе станции Белграда и БГ:ВОЗ; пока только `page_change_detection`, `auto_publish: false`;
- `welcome_serbia_temporary_residence` — официальный портал Government of Serbia с информацией для
  иностранцев о временном проживании и единой разрешительной процедуре. Это
  `high_risk: true`, `auto_publish: false`: изменения миграционных правил никогда не превращаются
  в публикацию без отдельной редакционной проверки.

Для обоих источников whole-page snapshot остаётся лишь сигналом изменения. Структурированный
article/guidance parser можно включать только после сохранения живых fixture'ов и проверки
семантики дат, заголовков и canonical URL.

Для `srbijavoz_news` подготовлен fail-closed item-level parser: archive page только обнаруживает
article URL, а evidence строится из полного текста статьи с явной publication date и canonical
identity. Публичный web crawl 22.10.2026 подтверждает, что официальный сайт действительно
публикует отдельные статьи с датами. Но GitHub-hosted runners в двух Azure-регионах получили
`ConnectError` после трёх попыток, поэтому этот parser намеренно не активирован в registry.
Активация разрешена только после успешного source-specific online probe без transport error;
зелёный общий `source-health`, который терпит transport errors, достаточным доказательством не
считается.

Each source file captures:
- stable source id;
- publisher and ownership/type;
- URL;
- language;
- categories;
- check interval;
- trust level;
- extraction strategy;
- high-risk policy where applicable.
