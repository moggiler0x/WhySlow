// Проверка подписки для других эндпоинтов сервера (например, сравнения с похожими ПК).
//
//   import { requireLicense } from "./auth.js";
//   const a = await requireLicense(request, env);
//   if (!a.ok) return a.response;
//   a.lic — данные лицензии: {kid, plan, exp, sub, dev}
//
// Программа шлёт заголовок «Authorization: Bearer WS1....» (license.auth_headers()).

import { readToken } from "./crypto.js";
import { getRecord } from "./licenses.js";

export function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });
}

export async function requireLicense(request, env) {
  const m = /^Bearer\s+(\S+)$/.exec(request.headers.get("Authorization") || "");
  const lic = m && (await readToken(env, m[1]));
  if (!lic) return { ok: false, response: json({ error: "no_license" }, 401) };
  const device = request.headers.get("X-WhySlow-Device");
  if (lic.dev?.length && device && !lic.dev.includes(device)) {
    return { ok: false, response: json({ error: "device" }, 403) };
  }
  if (lic.src === "online") {
    const rec = await getRecord(env, lic.kid);
    if (!rec || rec.revoked || rec.exp < Date.now() / 1000) return { ok: false, response: json({ error: "revoked" }, 403) };
  }
  return { ok: true, lic };
}
