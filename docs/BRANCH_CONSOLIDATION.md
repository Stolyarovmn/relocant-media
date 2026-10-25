# Сведение веток перед продолжением в Codex

26 октября 2026 года, по просьбе владельца о слиянии веток и удалении слитых refs.

## Результат и границы

Рабочая реализация взята из `main` после слияния PR #150: `c1feb0b02bec74c5b119035a46dadd5d0f7879ab`. Его push CI [40241949865](https://github.com/Stolyarovmn/relocant-media/actions/runs/40241949865) завершился успешно; Pages [40241949925](https://github.com/Stolyarovmn/relocant-media/actions/runs/40241949925) также опубликован успешно. В PR #150 разрешён конфликт с PR #151, сохранены уникальные имена Pages artifacts и добавлен regression test. Локально: Ruff, mypy и 911 тестов прошли; 88 integration tests пропущены без локальной PostgreSQL. Полный GitHub CI использовал PostgreSQL.

Сведение истории не включает новые production-запуски, публикации, изменение секретов или cutover. Actions + Drive ещё требуют эксплуатационного подключения по `ACTIONS_RUNTIME.md`.

Этот merge сохраняет SHA всех 94 прежних рабочих веток как родителей, включая ветки, ранее слитые squash. Поэтому после удаления refs все исходные коммиты остаются достижимыми из main. Код текущего main сохранён: уже заменённые и незавершённые варианты не накладываются поверх более новых guards. Единственный новый файл этого сведения — данный журнал.

`main` и `newsroom-data` сохраняются. `newsroom-data` — рабочие данные Pages, а не временная ветка разработки. Перед удалением проверяются точный SHA ветки, достижимость коммита из итогового main, отсутствие открытых PR и зелёный push CI точного main. Служебная ветка очистки удаляет себя после выполнения. Настройки branch protection не меняются.

## Ветки, требовавшие отдельного решения

- `diag/fresh-source-health`: Одноразовый диагностический комментарий; обычный source-health сохранён.
- `diag/tls-system-trust`: Временные сетевые TLS-пробы в CI; не возвращаются в штатные проверки.
- `feat/media-release-preparation`: Альтернативная подготовка медиа заменена текущим media_release_finalizer; guards и актуальная CLI сохранены.
- `feat/recommendation-attribution`: Незавершённая заготовка: модели, сервис и миграция с занятым номером 0013. Сохранена в истории; не активирована.
- `feat/server-cycle-structured-ingestion`: Незавершённая заготовка пассивного ingestion и optional candidate gate без подключения к runtime. Сохранена в истории; не активирована.
- `feat/sqlite-actions-state`: Старый artifact-based SQLite-контур заменён адаптером Actions + Drive из PR #150. Второй писатель не добавлен.
- `feat/welcome-serbia-guidance`: Заменена слитой реализацией welcome-serbia-guidance-v2; текущие source policies сохранены.
- `source/novi-sad-messenger-candidate`: Заменена отдельным каталогом Нови-Сада; смешение городов не возвращается.
- `feat/high-risk-owner-review-loop-2`: Коммит уже является предком main.

Незавершённые заготовки не объявлены реализованными. Для продолжения используйте SHA ниже: `git show <SHA>:<путь>` или отдельную новую ветку с выборочным переносом и проверками. Не возвращайте старый номер миграции 0013 и не включайте второй редакционный писатель.

## Исходные SHA для восстановления

| Ветка | SHA | Основание |
|---|---|---|
| `diag/fresh-source-health` | `ccabd3eff09d689dcac5e962120228a5e9ab005d` | Сведена история; решение выше |
| `diag/tls-system-trust` | `0a2ab939923fb536ce7ba58eed345fa7142f4019` | Сведена история; решение выше |
| `docs/messenger-discovery-summary` | `0f908a86b2b312e79b40a6da204368250301dcdb` | [PR #134](https://github.com/Stolyarovmn/relocant-media/pull/134), слит |
| `docs/operational-sync-20261021` | `6ff82699c263d5353260451eb67dda5a78ace622` | [PR #58](https://github.com/Stolyarovmn/relocant-media/pull/58), слит |
| `docs/sync-operational-state-after-76` | `e37bb8dd7f492f9b41dd42139559b614bc84dd7c` | [PR #78](https://github.com/Stolyarovmn/relocant-media/pull/78), слит |
| `docs/verified-systemd-jev-runtime` | `68a63b5b20f441f9b56b34afa7dfdaef4bf1cc2d` | [PR #146](https://github.com/Stolyarovmn/relocant-media/pull/146), слит |
| `feat/actions-drive-runtime` | `de96ab8e3950fd180bea6dcbd1e904533a78c116` | [PR #150](https://github.com/Stolyarovmn/relocant-media/pull/150), слит |
| `feat/ad-pricing-policy` | `24350ed0288ce1ca51b9c87debaf4f9f5a1e1418` | [PR #67](https://github.com/Stolyarovmn/relocant-media/pull/67), слит |
| `feat/ad-staging-e2e` | `98db7a9f1f86d2b25721d8312d4b3ed6fbd27b73` | [PR #68](https://github.com/Stolyarovmn/relocant-media/pull/68), слит |
| `feat/belgrade-campaign-brief` | `1535692c30b93fc86cd46fc7f1054d541ce5ea85` | [PR #147](https://github.com/Stolyarovmn/relocant-media/pull/147), слит |
| `feat/belgrade-parking-heating-sources` | `10955beb90790d4e6002ae32467b2a76f3dae3c9` | [PR #70](https://github.com/Stolyarovmn/relocant-media/pull/70), слит |
| `feat/belgrade-utility-source-snapshots` | `c37cb1ad7a44448abc7693cdebc80acc2a5f434c` | [PR #69](https://github.com/Stolyarovmn/relocant-media/pull/69), слит |
| `feat/beoelektrane-structured-works` | `873962ef9f0e2c7510731a58a7035ed6fe14c586` | [PR #101](https://github.com/Stolyarovmn/relocant-media/pull/101), слит |
| `feat/bgprevoz-structured-changes` | `1f47ac288d17685b6c7b7a5e85eb1a18ca50845d` | [PR #104](https://github.com/Stolyarovmn/relocant-media/pull/104), слит |
| `feat/bgprevoz-structured-news` | `d0bfd387d59d9cf53abe6852f6fc7b0a735d5adc` | [PR #105](https://github.com/Stolyarovmn/relocant-media/pull/105), слит |
| `feat/bitefartcafe-structured-concerts` | `57f72e00555211a1007981e9368ecc0227449430` | [PR #115](https://github.com/Stolyarovmn/relocant-media/pull/115), слит |
| `feat/bvk-structured-outages` | `1308be7261a0e7bc6d08bc55f64799562f57ab6b` | [PR #73](https://github.com/Stolyarovmn/relocant-media/pull/73), слит |
| `feat/category-learning-report` | `b095380a9c7784d13544ce90b8d78a0bf3eaf282` | [PR #93](https://github.com/Stolyarovmn/relocant-media/pull/93), слит |
| `feat/compact-source-links` | `6c29c576f9a8a2c75d2a8096429ee40ac1abdd5e` | [PR #76](https://github.com/Stolyarovmn/relocant-media/pull/76), слит |
| `feat/control-decision-cas` | `cf9bd51f1eb80a402f647ebff5cbf17334e03087` | [PR #81](https://github.com/Stolyarovmn/relocant-media/pull/81), слит |
| `feat/editorial-experiment-registry` | `85db5121ac736602f1e6d84d8fc1b70b2b919e68` | [PR #62](https://github.com/Stolyarovmn/relocant-media/pull/62), слит |
| `feat/editorial-media-provenance` | `4a9228a074abcb49ada477e8c9d4091b1cddd18c` | [PR #88](https://github.com/Stolyarovmn/relocant-media/pull/88), слит |
| `feat/eds-structured-outages` | `e740c2a252c7fff9916835c4b00881143fab447c` | [PR #74](https://github.com/Stolyarovmn/relocant-media/pull/74), слит |
| `feat/euprava-foreigner-service` | `ab0c52f9db0a95952a9a523d7cfb260d9adeafd8` | [PR #107](https://github.com/Stolyarovmn/relocant-media/pull/107), слит |
| `feat/event-lifecycle-source-updates` | `dd7a05c9889af03324654b157607ee0b8145fb1e` | [PR #72](https://github.com/Stolyarovmn/relocant-media/pull/72), слит |
| `feat/high-risk-owner-review-loop` | `8a3635f0433c92bb4ef2a51bfdff8ca377ee70b4` | [PR #126](https://github.com/Stolyarovmn/relocant-media/pull/126), слит |
| `feat/high-risk-owner-review-loop-2` | `94f827794c1a99911de5f332573b2a3e722205e4` | Сведена история; решение выше |
| `feat/jev-github-actions-wiring` | `40af330c6132391621fd097d1a6146d79fc72b3f` | [PR #142](https://github.com/Stolyarovmn/relocant-media/pull/142), слит |
| `feat/jev-shadow-single-switch` | `cb91374f6994a300fc4ba32a5206901d4d8982d8` | [PR #140](https://github.com/Stolyarovmn/relocant-media/pull/140), слит |
| `feat/media-release-finalizer` | `c5ad7770e91e1c1ae1fef6815471f307f40e9d4d` | [PR #96](https://github.com/Stolyarovmn/relocant-media/pull/96), слит |
| `feat/media-release-preparation` | `524730bd9fdcb2a443d94f9d5c53109b7498a538` | Сведена история; решение выше |
| `feat/media-review-packets` | `1d07f54cdafb6c4e8883736366f986dbacdd9a94` | [PR #91](https://github.com/Stolyarovmn/relocant-media/pull/91), слит |
| `feat/moderation-pipeline-v2` | `aa9f3f100435f276766d4c5df01b2ef1c2e03c4a` | [PR #60](https://github.com/Stolyarovmn/relocant-media/pull/60), слит |
| `feat/musicology-structured-blog` | `a453c061a94e4c22200f29ed4198c8b821695267` | [PR #116](https://github.com/Stolyarovmn/relocant-media/pull/116), слит |
| `feat/natural-history-html-news` | `9cd0beb3bd6a1469b514ecf738c5318cead2dd4e` | [PR #113](https://github.com/Stolyarovmn/relocant-media/pull/113), слит |
| `feat/newsroom-control-plane` | `9861e1f467c8f8d40b5d2770c1c58715d402ba27` | [PR #59](https://github.com/Stolyarovmn/relocant-media/pull/59), слит |
| `feat/newsroom-pages` | `edd3028e65d61cc7eefa9b7c7c45684204092c2c` | [PR #148](https://github.com/Stolyarovmn/relocant-media/pull/148), слит |
| `feat/official-vertical-sources` | `9ed494116ee3a7cdb6417428ea6b93f12c8ad0ff` | [PR #92](https://github.com/Stolyarovmn/relocant-media/pull/92), слит |
| `feat/popular-channel-discovery-source-links` | `7821066fb128d14d90b83f2210d9b86e29a0b133` | [PR #84](https://github.com/Stolyarovmn/relocant-media/pull/84), слит |
| `feat/popular-telegram-discovery` | `bea92def5a06e9ddeba49a50295a91ecea554d55` | [PR #75](https://github.com/Stolyarovmn/relocant-media/pull/75), слит |
| `feat/production-media-install` | `af2003eb15c369be2d57aec1add4237298ff0f39` | [PR #95](https://github.com/Stolyarovmn/relocant-media/pull/95), слит |
| `feat/production-reviewed-media` | `2a6606a7f14e7252e7b2bdfaef9127c5bfbb8960` | [PR #94](https://github.com/Stolyarovmn/relocant-media/pull/94), слит |
| `feat/read-only-ads-analyst` | `f4096940742061b217c522f2711eb33ba9e5f2dc` | [PR #63](https://github.com/Stolyarovmn/relocant-media/pull/63), слит |
| `feat/reader-correction-audit` | `c26c7ae2de776afdf12948c57592e56baf29a1b7` | [PR #65](https://github.com/Stolyarovmn/relocant-media/pull/65), слит |
| `feat/real-media-candidate-extraction` | `7e9744a4ec6fb90dc788a4091ff76fa06f1bf112` | [PR #86](https://github.com/Stolyarovmn/relocant-media/pull/86), слит |
| `feat/real-primary-source-media` | `4031defaaa421c54b032d06f21cc982bb1caf1da` | [PR #77](https://github.com/Stolyarovmn/relocant-media/pull/77), слит |
| `feat/recommendation-attribution` | `bfdf324881b0c03d5b8512e0b0e4fb273231349e` | Сведена история; решение выше |
| `feat/reviewed-media-materialization` | `aa55baf6111e479d1c41db7ad4d9710c4eeb9a58` | [PR #98](https://github.com/Stolyarovmn/relocant-media/pull/98), слит |
| `feat/safe-server-deploy-main` | `7374fb45b7c3334d6cca754ac3fb1b7bf3c34514` | [PR #122](https://github.com/Stolyarovmn/relocant-media/pull/122), слит |
| `feat/saved-html-media-manifest` | `d1f66961d1c63f603246562e0c651db4c8fdbe32` | [PR #87](https://github.com/Stolyarovmn/relocant-media/pull/87), слит |
| `feat/self-authored-fallback-card` | `8200f2652653e6735c992de34b1d1a0e1b4acced` | [PR #125](https://github.com/Stolyarovmn/relocant-media/pull/125), слит |
| `feat/serbian-news-discovery-registry` | `67ee7d646880868d685a084888769690ca95cb1b` | [PR #85](https://github.com/Stolyarovmn/relocant-media/pull/85), слит |
| `feat/serbian-rss-discovery` | `ee79b3b3a69d9232b59942f746b6e45214bc0e44` | [PR #89](https://github.com/Stolyarovmn/relocant-media/pull/89), слит |
| `feat/server-cycle-structured-ingestion` | `0e1535c5c82b46e3b1c0ee1d4f7d34f69b7de229` | Сведена история; решение выше |
| `feat/shadow-scorer-provenance` | `477a10ceaab4a713b2bb4f46cbd686dbb2e1e52f` | [PR #80](https://github.com/Stolyarovmn/relocant-media/pull/80), слит |
| `feat/shadow-semantic-dedup` | `deea5da84a14a184854658b6112e58742789f06b` | [PR #61](https://github.com/Stolyarovmn/relocant-media/pull/61), слит |
| `feat/source-revision-review-control` | `a6689d654839828625beb860bd962023166c2aa4` | [PR #64](https://github.com/Stolyarovmn/relocant-media/pull/64), слит |
| `feat/source-yield-analytics` | `b90b4ec3c1f2cb8c9c0780f51a18c1eefb65c56c` | [PR #71](https://github.com/Stolyarovmn/relocant-media/pull/71), слит |
| `feat/sqlite-actions-state` | `ed3c46cce10e8064573fd52bc9b8beaeb0964086` | Сведена история; решение выше |
| `feat/srbijavoz-structured-news` | `066beccd64cbe620942ec7a06056223b9d7cfd68` | [PR #99](https://github.com/Stolyarovmn/relocant-media/pull/99), слит |
| `feat/strict-changed-source-health` | `9336c52e884120a150884ef18998697e671117c0` | [PR #100](https://github.com/Stolyarovmn/relocant-media/pull/100), слит |
| `feat/structured-health-duty` | `86e956132b7593ad1ee9ee221db62c6c4d149d17` | [PR #106](https://github.com/Stolyarovmn/relocant-media/pull/106), слит |
| `feat/structured-venue-server-cycle` | `0d3a147b9c1a89612c867766a912d5fc63224e16` | [PR #120](https://github.com/Stolyarovmn/relocant-media/pull/120), слит |
| `feat/suggested-post-review` | `1a9a055d052c5116a3b26481919e4110f71a9908` | [PR #66](https://github.com/Stolyarovmn/relocant-media/pull/66), слит |
| `feat/tob-structured-events` | `3688e559486358839ae7e7708549172be292e782` | [PR #108](https://github.com/Stolyarovmn/relocant-media/pull/108), слит |
| `feat/undated-venue-review-holds` | `20010fc43e2712baf590c82bc5583da065e1ffb0` | [PR #123](https://github.com/Stolyarovmn/relocant-media/pull/123), слит |
| `feat/venue-extraction-reasons` | `0cd8d4a33a67472b1ca4f5c3e44249fb7c891c8f` | [PR #103](https://github.com/Stolyarovmn/relocant-media/pull/103), слит |
| `feat/venue-item-failure-diagnostics` | `882342cfb994efc0c57960d8f4e0e2df6db705a8` | [PR #102](https://github.com/Stolyarovmn/relocant-media/pull/102), слит |
| `feat/welcome-serbia-guidance` | `19d477b92453224abd8930f8b6dfbb2516af8073` | Сведена история; решение выше |
| `feat/welcome-serbia-guidance-v2` | `ad458031af8ce2ff9ca829e424081218c6f3ab3b` | [PR #111](https://github.com/Stolyarovmn/relocant-media/pull/111), слит |
| `feat/yugoslavia-museum-html-programs` | `1fc7d54f2fb699eefee0f15c172a4393364bf1b5` | [PR #112](https://github.com/Stolyarovmn/relocant-media/pull/112), слит |
| `fix/compact-source-links` | `6cf21beb1526c44c36b7f5166be0c1183b6ae36d` | [PR #83](https://github.com/Stolyarovmn/relocant-media/pull/83), слит |
| `fix/jev-actions-smoke-runtime-audit` | `304ef3ba2fd551f639484210e664e5cfee2ef6a1` | [PR #144](https://github.com/Stolyarovmn/relocant-media/pull/144), слит |
| `fix/jev-server-runtime` | `dad0a424189110642a9e2a91d44c88757a69cc9e` | [PR #143](https://github.com/Stolyarovmn/relocant-media/pull/143), слит |
| `fix/newsroom-pages-setup` | `427ed47b587bdfa3c4eb1896fc5d96c9e3c2abd9` | [PR #149](https://github.com/Stolyarovmn/relocant-media/pull/149), слит |
| `fix/owner-review-safe-diagnostics` | `576cb3c57dd11e7f20e0762fa4f57920bdaa99e5` | [PR #127](https://github.com/Stolyarovmn/relocant-media/pull/127), слит |
| `fix/pages-snapshot-artifacts` | `71c45f497dab680b418a45d98088182532aadfe4` | [PR #151](https://github.com/Stolyarovmn/relocant-media/pull/151), слит |
| `fix/production-runtime-freshness` | `4345a675ff5e5638a43c930604573a4a58c63bb0` | [PR #121](https://github.com/Stolyarovmn/relocant-media/pull/121), слит |
| `fix/quarantine-unsafe-venue-feeds` | `328ca9d2271cf0d6a61ef2fdb8ba00dad231769d` | [PR #110](https://github.com/Stolyarovmn/relocant-media/pull/110), слит |
| `fix/same-publisher-transport-reprints` | `5072e7efc5b7b0998a2d45d3f3f328578e2f1b5f` | [PR #117](https://github.com/Stolyarovmn/relocant-media/pull/117), слит |
| `fix/venue-article-lead` | `62b0cc0b99fe21f8268521ba993305df14b7f2ef` | [PR #82](https://github.com/Stolyarovmn/relocant-media/pull/82), слит |
| `ops/auto-deploy-green-main` | `afbe12d926b846adc70ecb756d20174608ca5d12` | [PR #124](https://github.com/Stolyarovmn/relocant-media/pull/124), слит |
| `ops/real-source-health` | `604efb9d0c8099e959f0775103607b4977b93305` | [PR #79](https://github.com/Stolyarovmn/relocant-media/pull/79), слит |
| `refactor/shared-discovery-resolution` | `bda1c96da08c60f30c6aa3465f18054a8284769f` | [PR #90](https://github.com/Stolyarovmn/relocant-media/pull/90), слит |
| `safety/city-channel-routing` | `931367181957fe260359f3d9c309adf4fdfe1b4c` | [PR #135](https://github.com/Stolyarovmn/relocant-media/pull/135), слит |
| `safety/city-scoped-messenger-discovery` | `2e7dff9e5c6a30f0b84b4838f1e61f4f6b4dc30b` | [PR #136](https://github.com/Stolyarovmn/relocant-media/pull/136), слит |
| `source/messenger-discovery` | `2224fb608b0df8cb1f1a4e5ea15118e338537228` | [PR #130](https://github.com/Stolyarovmn/relocant-media/pull/130), слит |
| `source/messenger-discovery-2` | `0c1760d6dbb138dada2f7d5226b4d15a1c8b7fe0` | [PR #131](https://github.com/Stolyarovmn/relocant-media/pull/131), слит |
| `source/messenger-discovery-n1-viber` | `e8c96326a6d2ac66f49055d25c3bd4a0e105e9df` | [PR #137](https://github.com/Stolyarovmn/relocant-media/pull/137), слит |
| `source/messenger-discovery-n1-whatsapp` | `295ba72adbc900909ad7f7cdd36ae40dfafa0e02` | [PR #132](https://github.com/Stolyarovmn/relocant-media/pull/132), слит |
| `source/messenger-discovery-nova-whatsapp` | `e5b13744c85b88600b1529ed7f40711f7268262a` | [PR #133](https://github.com/Stolyarovmn/relocant-media/pull/133), слит |
| `source/novi-sad-messenger-candidate` | `f2b01b6b0af7fb90ed9be2114ae8ef0569bd4b3f` | Сведена история; решение выше |
| `source/novi-sad-viber-catalog` | `b582edbdd19f988c300d68f66fb70d7e528e4699` | [PR #139](https://github.com/Stolyarovmn/relocant-media/pull/139), слит |
| `source/srbija-gov-high-risk` | `8126a5280c4331d38542001380de5d7998e50f5d` | [PR #129](https://github.com/Stolyarovmn/relocant-media/pull/129), слит |
