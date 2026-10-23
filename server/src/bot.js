// Telegram-бот: владелец выпускает и отзывает коды из чата, покупатели платят звёздами
// Telegram (XTR, провайдер не нужен) и сразу получают код. Остальные сообщения покупателей
// пересылаются владельцу, он отвечает /reply.

import { sameSecret } from "./crypto.js";
import { PLANS, createCode, extend, formatCode, getRecord, listRecords, now, resolveKid, setRevoked, unlink } from "./licenses.js";

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json().catch(() => ({}));
}

const say = (env, chat, text) => tg(env, "sendMessage", { chat_id: chat, text, disable_web_page_preview: true });

function date(ts) {
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow" });
}

function describe(rec) {
  const state = rec.revoked ? "ОТОЗВАН" : rec.exp < now() ? "истёк" : `до ${date(rec.exp)}`;
  const devs = rec.devices.map((d) => `${d.id}${d.name ? ` (${d.name})` : ""}`).join(", ") || "нет";
  const plan = rec.trial ? "пробный период" : PLANS[rec.plan]?.name || rec.plan;
  return `${rec.code || rec.kid} · ${plan} · ${state}\nустройства ${rec.devices.length}/${rec.max}: ${devs}` +
    (rec.note ? `\n${rec.note}` : "");
}

const OWNER_HELP = `Команды владельца:
/solo 1 заметка — код «Соло» на 1 месяц
/family 12 заметка — код «3 устройства» на 12 месяцев
/extend КОД 1 — продлить на месяц
/revoke КОД — отозвать
/unlink КОД XXXXX-XXXXX — отвязать устройство
/info КОД — состояние
/list — последние коды
/reply ID текст — ответить покупателю`;

function pricesText(env) {
  const lines = Object.values(PLANS).map((p) => `• ${p.name} — ${p.price} ⭐ в месяц, устройств: ${p.devices === 1 ? "1" : `до ${p.devices}`}`);
  const pay = "\n\nОплатить звёздами Telegram: /buy_solo или /buy_family — код придёт сюда сразу после оплаты.";
  const ask = env.OWNER_CONTACT ? `\nВопросы: ${env.OWNER_CONTACT}.` :
    env.OWNER_CHAT_ID ? "\nВопрос владельцу можно написать прямо сюда." : "";
  return `Подписка WhySlow открывает платные функции программы.\n\n${lines.join("\n")}\n\n` +
    "Сначала можно бесплатно попробовать 7 дней: в WhySlow «★ Подписка» → «Попробовать 7 дней бесплатно» " +
    `(один раз на компьютер).${pay}${ask}\n\n` +
    "Код вводится в WhySlow: кнопка «★ Подписка» внизу окна → поле «Ключ».";
}

async function ownerCommand(env, chat, cmd, args) {
  const months = (s, d = 1) => Math.max(0.03, Number(String(s || d).replace(",", ".")) || d);
  if (cmd === "/solo" || cmd === "/family") {
    const rec = await createCode(env, cmd.slice(1), months(args[0]) * 31, args.slice(1).join(" "));
    await say(env, chat, describe(rec));
    return say(env, chat, rec.code); // отдельным сообщением — удобно переслать покупателю
  }
  if (cmd === "/reply" && /^\d+$/.test(args[0] || "") && args.length > 1) {
    await say(env, args[0], args.slice(1).join(" "));
    return say(env, chat, "Отправлено");
  }
  if (cmd === "/list") {
    const items = (await listRecords(env)).slice(0, 30);
    return say(env, chat, items.map(describe).join("\n\n") || "Кодов пока нет");
  }
  const kid = await resolveKid(args[0]);
  if (!kid) return say(env, chat, OWNER_HELP);
  let rec;
  if (cmd === "/extend") rec = await extend(env, kid, months(args[1]) * 31);
  else if (cmd === "/revoke") rec = await setRevoked(env, kid, true);
  else if (cmd === "/unlink") rec = await unlink(env, kid, String(args[1] || "").toUpperCase());
  else if (cmd === "/info") rec = await getRecord(env, kid);
  else return say(env, chat, OWNER_HELP);
  return say(env, chat, rec ? describe(rec) : "Такого кода нет");
}

async function sendInvoice(env, chat, plan) {
  const p = PLANS[plan];
  return tg(env, "sendInvoice", {
    chat_id: chat, title: `WhySlow — ${p.name}`, description: `Подписка на 1 месяц, устройств: ${p.devices === 1 ? "1" : `до ${p.devices}`}`,
    payload: `sub:${plan}:1`, currency: "XTR", // звёзды Telegram: provider_token не нужен
    prices: [{ label: `${p.name}, 1 месяц`, amount: p.price }],
  });
}

async function paid(env, msg) {
  const pay = msg.successful_payment;
  const [, plan, monthsStr] = String(pay.invoice_payload).split(":");
  const p = PLANS[plan];
  const months = Number(monthsStr) || 1;
  // Код выдаём один раз на платёж и только если сумма совпала с тарифом
  const seen = `pay:${pay.telegram_payment_charge_id}`;
  if (!p || pay.currency !== "XTR" || pay.total_amount !== p.price * months || (await env.LICENSES.get(seen))) return;
  const who = msg.from?.username ? `@${msg.from.username}` : `id ${msg.from?.id}`;
  const rec = await createCode(env, plan, months * 31, `оплата в боте ${who}, ${pay.provider_payment_charge_id}`);
  await env.LICENSES.put(seen, rec.kid, { expirationTtl: 400 * 86400 });
  await say(env, msg.chat.id, `Спасибо! Ваш код подписки «${p.name}» до ${date(rec.exp)}:`);
  await say(env, msg.chat.id, rec.code);
  await say(env, msg.chat.id, "Откройте WhySlow → «Подписка» → вставьте код в поле «Ключ» → «Активировать».");
  if (env.OWNER_CHAT_ID) await say(env, env.OWNER_CHAT_ID, `Новая оплата от ${who}\n${describe(rec)}`);
}

// Сообщение владельцу в Telegram, если бот настроен. Ошибка связи не мешает основному ответу.
export async function notifyOwner(env, text) {
  if (!env.TG_TOKEN || !env.OWNER_CHAT_ID) return;
  try {
    await say(env, env.OWNER_CHAT_ID, text);
  } catch {
    // бот недоступен — не страшно
  }
}

export async function handleTelegram(request, env, pathSecret) {
  if (!env.TG_TOKEN || !sameSecret(pathSecret, env.TG_SECRET) ||
      !sameSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "", env.TG_SECRET)) {
    return new Response("not found", { status: 404 });
  }
  const upd = await request.json().catch(() => ({}));
  if (upd.pre_checkout_query) {
    const q = upd.pre_checkout_query;
    const [, plan, m] = String(q.invoice_payload).split(":");
    const ok = PLANS[plan] && q.currency === "XTR" && q.total_amount === PLANS[plan].price * (Number(m) || 1);
    await tg(env, "answerPreCheckoutQuery", { pre_checkout_query_id: q.id, ok: !!ok, error_message: ok ? undefined : "Неверная сумма" });
    return new Response("ok");
  }
  const msg = upd.message;
  if (!msg) return new Response("ok");
  if (msg.successful_payment) {
    await paid(env, msg);
    return new Response("ok");
  }
  const [cmd = "", ...args] = String(msg.text || "").trim().split(/\s+/);
  const command = cmd.split("@")[0].toLowerCase();
  const isOwner = env.OWNER_CHAT_ID && String(msg.from?.id) === String(env.OWNER_CHAT_ID);
  if (isOwner && command !== "/start" && command !== "/buy_solo" && command !== "/buy_family") {
    await ownerCommand(env, msg.chat.id, command, args);
  } else if (command === "/buy_solo" || command === "/buy_family") {
    await sendInvoice(env, msg.chat.id, command.slice(5));
  } else if (!isOwner && msg.text && !msg.text.startsWith("/") && env.OWNER_CHAT_ID) {
    // Не команда — вопрос покупателя, передаём владельцу
    const who = [msg.from?.first_name, msg.from?.username && `@${msg.from.username}`].filter(Boolean).join(" ");
    await say(env, env.OWNER_CHAT_ID, `Заявка от ${who || "покупателя"} (id ${msg.from?.id}):\n${msg.text}\n\nОтветить: /reply ${msg.chat.id} текст`);
    await say(env, msg.chat.id, "Передал владельцу, ответ придёт сюда.");
  } else {
    await say(env, msg.chat.id, pricesText(env) + (isOwner ? `\n\n${OWNER_HELP}` : ""));
  }
  return new Response("ok");
}

export { formatCode };
