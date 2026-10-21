# Сервер сравнения с похожими ПК

Модуль `src/compare.js` (клиент — `whyslow/premium/compare.py`) живёт в том же Cloudflare Worker, что и проверка подписки.

## Подключение

1. Создать базу D1 (один раз):

   ```
   npx wrangler d1 create whyslow-compare
   npx wrangler d1 execute whyslow-compare --remote --file=server/compare-schema.sql
   ```

2. В `wrangler.toml` добавить привязку (id — из вывода первой команды):

   ```toml
   [[d1_databases]]
   binding = "COMPARE_DB"
   database_name = "whyslow-compare"
   database_id = "<id>"
   ```

3. В роутере `src/index.js` (обёртку с `requireLicense` для `/compare` добавляет модуль подписки):

   ```js
   import { handleCompare, handleCompareForget } from "./compare.js";
   // ...
   if (url.pathname === "/compare" && request.method === "POST") {
     const a = await requireLicense(request, env);
     if (!a.ok) return a.response;
     return handleCompare(request, env, a.lic);
   }
   if (url.pathname === "/compare/forget" && request.method === "POST") {
     return handleCompareForget(request, env); // удалить свои данные можно и без подписки
   }
   ```

4. `npx wrangler deploy`.

## Что хранится

Таблица `snapshots`: случайный id установки, время, ОС, модель процессора и видеокарты,
ядра, память, тип диска, замеры и id программ из `known.json`. Ни IP, ни ключа подписки,
ни данных из токена (`a.lic`) в базе нет. Наружу — только сводка по группе из 5+ ПК.

## Тесты

```
cd server && npm test
```
