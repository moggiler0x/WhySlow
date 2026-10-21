// Сервер активации подписки WhySlow.
//
//   POST /v1/activate   {code, device, name}  → {token}   программа: первый ввод кода
//   POST /v1/refresh    {code, device}        → {token}   программа: продление раз в сутки
//   POST /v1/deactivate {code, device}        → {ok}      программа: «Отключить на этом компьютере»
//   POST /admin/*       (Authorization: Bearer ADMIN_TOKEN) — owner/keys.py
//   POST /tg/<TG_SECRET>                                  Telegram-бот
//
// Другие функции подключаются строкой в ROUTES; проверка подписки — requireLicense из auth.js.

import { json } from "./auth.js";
import { handleTelegram } from "./bot.js";
import { sameSecret } from "./crypto.js";
import { activate, createCode, extend, getRecord, listRecords, resolveKid, setRevoked, unlink } from "./licenses.js";
// import { handleCompareSubmit, handleCompareQuery } from "./compare.js";

const ROUTES = {
  // "/compare/submit": handleCompareSubmit,
  // "/compare/query": handleCompareQuery,
};

async function admin(request, env, action) {
  const m = /^Bearer\s+(\S+)$/.exec(request.headers.get("Authorization") || "");
  if (!env.ADMIN_TOKEN || !m || !sameSecret(m[1], env.ADMIN_TOKEN)) return json({ error: "forbidden" }, 403);
  const body = await request.json().catch(() => ({}));
  if (action === "create") {
    try {
      return json(await createCode(env, body.plan, Number(body.days) || 31, body.note));
    } catch (e) {
      return json({ error: String(e.message) }, 400);
    }
  }
  if (action === "list") return json({ items: await listRecords(env) });
  const kid = await resolveKid(body.code);
  let rec = null;
  if (action === "extend") rec = await extend(env, kid, Number(body.days) || 31);
  else if (action === "revoke") rec = await setRevoked(env, kid, true);
  else if (action === "unrevoke") rec = await setRevoked(env, kid, false);
  else if (action === "unlink") rec = await unlink(env, kid, String(body.device || ""));
  else if (action === "info") rec = await getRecord(env, kid);
  else return json({ error: "not_found" }, 404);
  return rec ? json(rec) : json({ error: "unknown" }, 404);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");
    if (path === "" || path === "/health") return json({ ok: true, service: "whyslow-license" });
    if (request.method !== "POST") return json({ error: "method" }, 405);
    if (!env.PUBLIC_KEY || !env.PRIVATE_KEY) return json({ error: "not_configured" }, 503);

    const v1 = /^\/v1\/(activate|refresh|deactivate)$/.exec(path);
    if (v1) {
      const body = await request.json().catch(() => ({}));
      const r = await activate(env, body, v1[1]);
      return r.error ? json({ error: r.error }, r.status) : json(r);
    }
    const adm = /^\/admin\/(\w+)$/.exec(path);
    if (adm) return admin(request, env, adm[1]);
    const tgm = /^\/tg\/([\w-]+)$/.exec(path);
    if (tgm) return handleTelegram(request, env, tgm[1]);
    if (ROUTES[path]) return ROUTES[path](request, env);
    return json({ error: "not_found" }, 404);
  },
};
