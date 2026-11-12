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

`outage-areas.json.gz` хранит адресные точки, контуры строений и проверяемые
границы поселений из того же снимка OSM. Если у контура несколько разных
адресов, для каждого остаётся точка. Совпадение проверяется по улице, номеру
и общине. Для поселения из объявления используется существующая именованная
граница OSM, если она однозначно относится к этой общине.

```sh
PYTHONPATH=src uv run --with osmium==4.3.2 --with shapely==2.1.2 python \
  -m relocant_media.ops.belgrade_outage_geometry \
  --input /path/to/serbia-latest.osm.pbf \
  --output cities/belgrade/map/outage-areas.json.gz
```

Сборщик запускается отдельно при обновлении снимка. Цикл редакции читает
готовый архив и публикует только объекты, связанные с конкретными объявлениями.
[Атрибуция и лицензия OSM](https://www.openstreetmap.org/copyright)

Если точный адрес отсутствует в OSM, `rgz-addresses.json.gz` позволяет использовать
координаты кадастра Сербии. В архиве сохраняются дата и адрес каждого отчёта, а также
кадастровый идентификатор каждой точки. Координата RGZ сама по себе не задаёт контур
здания. Первоначальная выгрузка включает Нови-Београд. Для других общин можно
повторить параметр `--report` с названием общины и точным заголовком её отчёта.
[Адресный регистр RGZ](https://www.rgz.gov.rs/adresni-registar) ·
[Публичный срез OSM Serbia](https://dina.openstreetmap.rs/ar/report.html)

```sh
report='https://dina.openstreetmap.rs/ar/opstine/novibeograd/BEOGRAD%20%28NOVI%20BEOGRAD%29.html'
curl --fail --location "$report" --output novi-beograd.html
PYTHONPATH=src python -m relocant_media.ops.belgrade_rgz_index \
  --report novi-beograd.html 'Novi Beograd' 'BEOGRAD (NOVI BEOGRAD)' "$report" \
  --output cities/belgrade/map/rgz-addresses.json.gz
```
