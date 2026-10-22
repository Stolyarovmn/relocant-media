# Подготовка reviewed media release

Этот контур закрывает промежуток между explicit cover/rights assessment и production installer. Он не публикует в Telegram, не выдаёт права использования сам и не заменяет два независимых media-review.

## 1. Explicit cover assessment

Сначала `relocant-media-review` экспортирует immutable media packet из сохранённого ingestion provenance. Reviewer заполняет `CoverAssessment`: event match, тип изображения, event year, visual value, способ/статус проверки прав, URL доказательства прав, rationale, attribution и reviewer id.

Только assessment с `rights_status=reviewed`, известным `rights_method`, evidence URL, rationale и attribution может попасть в cover plan. Неодобренные изображения остаются text-only и не скачиваются.

## 2. Prepare

Подготовка выполняется только при `APP_ENV=staging` и на существующей локальной SQLite:

```bash
relocant-media-release prepare \
  --raw-item-id 123 \
  --assessments /path/assessments.json \
  --event-year 2026 \
  --output /path/media-release-123
```

`--allow-album` разрешает существующему cover planner выбрать несколько изображений.

Prepare заново строит review packet из БД, заново применяет cover policy и скачивает только выбранные кандидаты. Для download используются исходный candidate URL и `approved_image_hosts`, сохранённые ingestion snapshot; новый произвольный host на этом этапе добавить нельзя. Redirect, размер и JPEG/PNG signature проверяются существующим generic media downloader.

В output создаются:

- `assets/` — скачанные файлы с content hash в имени;
- `release-draft.json` — production target, текущий package SHA, exact current editorial caption и assets;
- `media-review-template.json` — два hold-шаблона `facts` и `utility`, привязанные к точным asset hashes, caption hash и context hash;
- `preparation.json` — audit trail cover plan и explicit assessments.

Prepare не создаёт `release.json`. `publication_permitted=false`, Telegram I/O отсутствует.

Для внешних фотографий явный reviewed event year переносится в `MediaAsset`; если reviewer не задал отдельную подпись года для current photo, используется детерминированная подпись `Фото <event_year> года`. Это не определяет дату автоматически: значение берётся только из reviewed assessment.

## 3. Два независимых media-review

Два разных reviewer заполняют шаблоны ролей `facts` и `utility`. Для допуска оба review должны иметь:

- разные `reviewer_id`;
- `verdict=approve`;
- score не ниже 75;
- пустые flags;
- `live_source_checked=true`;
- timestamp не старше 24 часов;
- неизменённые reviewed asset hashes, caption SHA и context SHA.

## 4. Seal

```bash
relocant-media-release seal \
  --bundle /path/media-release-123 \
  --reviews /path/completed-media-reviews.json
```

Seal не использует сеть. Он требует ровно два review, проверяет точный набор локальных assets и запускает тот же `validate_media`, который используется production delivery. Только после успешной проверки эксклюзивно создаётся `release.json`.

Если asset, caption, rights material или review изменились после проверки, seal блокируется. Повторное sealing существующего `release.json` также блокируется.

## 5. Production install

Полученный bundle передаётся существующему production-only installer:

```bash
relocant-media-install \
  --release /path/media-release-123/release.json \
  --assets /path/media-release-123/assets
```

Installer ещё раз сверяет release с текущим READY editorial package/caption/production target и только затем атомарно устанавливает его в production media inbox. Отправка в Telegram происходит позже через обычный `deliver_reviewed` и его at-most-once DB receipt.
