// Коды активации и их устройства. В базе лежит не сам код, а его хеш (kid):
// даже утечка базы не даст готовых кодов.

import { featureKeys, makeToken, sha256hex } from "./crypto.js";

export const PLANS = {
  // price — в звёздах Telegram (XTR) за месяц
  solo: { name: "Соло", price: 50, devices: 1 },
  family: { name: "3 устройства", price: 120, devices: 3 },
};
// Платные функции — тот же список, что whyslow/license.py FEATURES. Любая подписка открывает все.
export const FEATURES = ["twin", "health_money", "xray", "gamemode", "compare", "pests", "migrate"];
const ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const TOKEN_DAYS = 7; // столько лицензия работает без связи с сервером
export const TRIAL_DAYS = 7; // бесплатный пробный период, один раз на компьютер
const TRIALS_PER_IP = 3; // сколько пробных периодов в сутки можно взять с одного адреса
const DAY = 86400;

export const now = () => Math.floor(Date.now() / 1000);

export function normalizeCode(text) {
  const s = String(text || "").toUpperCase().replace(/[\s\-_]/g, "").replace(/^WS/, "");
  return new RegExp(`^[${ALPHABET}]{16}$`).test(s) ? s : null;
}

export function formatCode(code) {
  return "WS-" + code.match(/.{4}/g).join("-");
}

export function newCode() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((b) => ALPHABET[b & 31]).join("");
}

export async function kidOf(code) {
  return (await sha256hex("whyslow-code:" + code)).slice(0, 16);
}

// Принимает код (WS-...) или его kid из списка
export async function resolveKid(text) {
  if (/^[0-9a-f]{16}$/.test(text || "")) return text;
  const code = normalizeCode(text);
  return code ? kidOf(code) : null;
}

export async function getRecord(env, kid) {
  return kid ? env.LICENSES.get("lic:" + kid, "json") : null;
}

export async function putRecord(env, rec) {
  // Запись живёт ещё год после конца подписки, потом KV удалит её сам
  await env.LICENSES.put("lic:" + rec.kid, JSON.stringify(rec), { expiration: Math.max(rec.exp, now()) + 365 * DAY });
}

export async function createCode(env, plan, days, note = "", extra = {}) {
  if (!PLANS[plan]) throw new Error("неизвестный тариф");
  const code = newCode();
  const rec = {
    kid: await kidOf(code), plan, exp: now() + Math.round(days * DAY), max: PLANS[plan].devices,
    devices: [], revoked: false, note: String(note).slice(0, 200), created: now(), ...extra,
  };
  await putRecord(env, rec);
  return { ...rec, code: formatCode(code) };
}

export async function extend(env, kid, days) {
  const rec = await getRecord(env, kid);
  if (!rec) return null;
  rec.exp = Math.max(rec.exp, now()) + Math.round(days * DAY);
  rec.revoked = false;
  await putRecord(env, rec);
  return rec;
}

export async function setRevoked(env, kid, revoked = true) {
  const rec = await getRecord(env, kid);
  if (!rec) return null;
  rec.revoked = revoked;
  await putRecord(env, rec);
  return rec;
}

export async function unlink(env, kid, device) {
  const rec = await getRecord(env, kid);
  if (!rec) return null;
  rec.devices = rec.devices.filter((d) => d.id !== device);
  await putRecord(env, rec);
  return rec;
}

export async function listRecords(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.LICENSES.list({ prefix: "lic:", cursor });
    for (const k of page.keys) {
      const rec = await env.LICENSES.get(k.name, "json");
      if (rec) out.push(rec);
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out.sort((a, b) => b.created - a.created);
}

// Активация и продление из программы. Возвращает {token} или {error, status}.
export async function activate(env, { code, device, name }, mode) {
  const c = normalizeCode(code);
  if (!c || !/^[A-Z0-9]{5}-[A-Z0-9]{5}$/.test(device || "")) return { status: 400, error: "bad_request" };
  const rec = await getRecord(env, await kidOf(c));
  if (!rec) return { status: 404, error: "unknown" };
  if (mode === "deactivate") {
    // Пробный период нельзя «отключить» и унести на другой компьютер
    if (!rec.trial) rec.devices = rec.devices.filter((d) => d.id !== device);
    await putRecord(env, rec);
    return { ok: true };
  }
  if (rec.revoked) return { status: 403, error: "revoked" };
  if (rec.exp < now()) return { status: 403, error: rec.trial ? "trial_over" : "expired" };
  let dev = rec.devices.find((d) => d.id === device);
  if (!dev) {
    if (rec.trial) return { status: 403, error: "trial_device" };
    if (mode === "refresh") return { status: 403, error: "device" };
    if (rec.devices.length >= rec.max) return { status: 409, error: "limit" };
    dev = { id: device, name: String(name || "").slice(0, 40), at: now() };
    rec.devices.push(dev);
  }
  dev.seen = now();
  await putRecord(env, rec);
  return { token: await issueToken(env, rec, device) };
}

async function issueToken(env, rec, device) {
  return makeToken(env, {
    v: 1, kid: rec.kid, plan: rec.plan, iat: now(), exp: Math.min(rec.exp, now() + TOKEN_DAYS * DAY),
    sub: rec.exp, max: rec.max, dev: [device], src: "online", keys: await featureKeys(env, FEATURES),
    ...(rec.trial ? { trial: 1 } : {}),
  });
}

// Компьютер узнаём по хешам серийников железа, которые прислала программа (hw). Каждый хеш
// хешируем ещё раз и храним вечно как «trial:<хеш>»: сами серийники сервер не видит никогда.
const trialKey = async (h) => "trial:" + (await sha256hex("whyslow-trial:" + h));

// Пробный период из программы: {device, hw: [sha256 hex], name} → {code, token} или {error, status}
export async function startTrial(env, { device, hw, name }, ip = "") {
  const hashes = [...new Set(Array.isArray(hw) ? hw : [])].filter((h) => /^[0-9a-f]{64}$/.test(h)).slice(0, 8);
  if (!/^[A-Z0-9]{5}-[A-Z0-9]{5}$/.test(device || "") || !hashes.length) return { status: 400, error: "bad_request" };
  const keys = await Promise.all(hashes.map(trialKey));
  for (const k of keys) if (await env.LICENSES.get(k)) return { status: 409, error: "trial_used" };
  if (ip) {
    const ipKey = "trialip:" + (await sha256hex("whyslow-ip:" + ip));
    const n = Number(await env.LICENSES.get(ipKey)) || 0;
    if (n >= TRIALS_PER_IP) return { status: 429, error: "busy" };
    await env.LICENSES.put(ipKey, String(n + 1), { expirationTtl: DAY });
  }
  const { code, ...rec } = await createCode(env, "solo", TRIAL_DAYS, "пробный период", {
    trial: true, devices: [{ id: device, name: String(name || "").slice(0, 40), at: now(), seen: now() }],
  });
  const mark = JSON.stringify({ kid: rec.kid, at: now() });
  for (const k of keys) await env.LICENSES.put(k, mark);
  return { code, token: await issueToken(env, rec, device) };
}
