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
