# Адресный индекс Белграда

Архив `addresses.json.gz` содержит производную базу OpenStreetMap под лицензией
ODbL 1.0. Источник и дата снимка, контрольная сумма исходного PBF и ссылка на
лицензию указаны в поле `source` самого JSON. GitHub Pages публикует распакованный
`map/addresses.json`. [Атрибуция и лицензия](https://www.openstreetmap.org/copyright) ·
[Выгрузки Geofabrik](https://download.geofabrik.de/europe/serbia.html)

В индекс попадают явно отмеченные номера домов, привязанные к улице или
населённому пункту внутри 17 городских общин. Координата соответствует узлу
OpenStreetMap или внутренней точке адресного полигона. Покрытие зависит от
полноты адресной разметки OpenStreetMap.

Чтобы обновить архив, скачайте выгрузку Сербии и выполните команду из корня репозитория.
Операция читает локальный PBF и не делает запросов к геокодерам.

```sh
uv run --with osmium==4.3.2 --with shapely==2.1.2 python \
  -m relocant_media.ops.belgrade_address_index \
  --input /path/to/serbia-latest.osm.pbf \
  --output cities/belgrade/map/addresses.json.gz \
  --roads-output cities/belgrade/map/roads.json.gz
```

`roads.json.gz` содержит именованные дороги и пешеходные пути с исходными
идентификаторами узлов OSM. В индекс попадают только линии, целиком лежащие
в одной общине. Эти данные помогают подтвердить геометрию участка между
перекрёстками, указанными в официальном объявлении.
