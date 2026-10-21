# Сервер сравнения с похожими ПК

Модуль `src/compare.js` (клиент — `whyslow/premium/compare.py`) живёт в том же Cloudflare Worker, что и проверка подписки.

## Подключение

Маршруты уже есть в `src/index.js`: `POST /compare` (только с подпиской) и `POST /compare/forget`
(без подписки). Пока база не подключена, они отвечают 503. Один раз, из папки `server`:

```
npx wrangler d1 create whyslow-compare
```

Скопируйте `database_id` из вывода в `wrangler.toml` вместо `REPLACE_WITH_D1_ID`, затем:

```
npx wrangler d1 execute whyslow-compare --remote --file=compare-schema.sql
npx wrangler deploy
```

## Что хранится

Таблица `snapshots`: случайный id установки, время, ОС, модель процессора и видеокарты,
ядра, память, тип диска, замеры и id программ из `known.json`. Ни IP, ни ключа подписки,
ни данных из токена (`a.lic`) в базе нет. Наружу — только сводка по группе из 5+ ПК.

## Тесты

```
cd server && npm test
```
