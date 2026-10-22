import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../src/index.js";

function fakeKV() {
  const m = new Map();
  return {
    m,
    async get(k, type) { const v = m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; },
    async put(k, v) { m.set(k, v); },
    async list({ prefix }) { return { keys: [...m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true }; },
  };
}

const env = () => ({
  LICENSES: fakeKV(), TG_TOKEN: "t", TG_SECRET: "hook-secret", OWNER_CHAT_ID: "42",
  PUBLIC_KEY: "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
  PRIVATE_KEY: "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
  SEAL_SECRET: "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
});

const sent = [];
globalThis.fetch = async (url, init) => {
  sent.push({ method: url.split("/").pop(), ...JSON.parse(init.body) });
  return new Response("{}");
};

const hook = (e, update, secret = "hook-secret") => worker.fetch(new Request(`https://x.dev/tg/${secret}`, {
  method: "POST", body: JSON.stringify(update), headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
}), e);

test("бот без секрета не отвечает", async () => {
  assert.equal((await hook(env(), {}, "wrong")).status, 404);
});

test("владелец выпускает код, чужой — нет", async () => {
  const e = env();
  sent.length = 0;
  await hook(e, { message: { chat: { id: 7 }, from: { id: 7 }, text: "/solo 1" } });
  assert.equal([...e.LICENSES.m.keys()].length, 0);
  await hook(e, { message: { chat: { id: 42 }, from: { id: 42 }, text: "/solo 1 Иван" } });
  assert.equal([...e.LICENSES.m.keys()].length, 1);
  assert.match(sent.at(-1).text, /^WS-/);
});

test("оплата в боте: код один раз и только за верную сумму", async () => {
  const e = env();
  sent.length = 0;
  const pay = (amount, id) => hook(e, { message: { chat: { id: 7 }, from: { id: 7, username: "buyer" }, successful_payment: {
    currency: "XTR", total_amount: amount, invoice_payload: "sub:solo:1", telegram_payment_charge_id: id, provider_payment_charge_id: "x" } } });
  await pay(5, "a");
  assert.equal(sent.length, 0);
  await pay(50, "b");
  await pay(50, "b");
  assert.equal([...e.LICENSES.m.keys()].filter((k) => k.startsWith("lic:")).length, 1);
  assert.ok(sent.some((m) => m.chat_id === 7 && /^WS-/.test(m.text)));
  assert.ok(sent.some((m) => m.chat_id === "42"));
});

test("вопрос покупателя уходит владельцу, он отвечает /reply", async () => {
  const e = env();
  sent.length = 0;
  await hook(e, { message: { chat: { id: 7 }, from: { id: 7, first_name: "Иван", username: "ivan" }, text: "/start" } });
  assert.match(sent.at(-1).text, /50 ⭐/);
  assert.match(sent.at(-1).text, /написать прямо сюда/);
  await hook(e, { message: { chat: { id: 7 }, from: { id: 7, first_name: "Иван", username: "ivan" }, text: "Хочу Соло" } });
  assert.ok(sent.some((m) => m.chat_id === "42" && /Хочу Соло/.test(m.text) && /\/reply 7/.test(m.text)));
  assert.equal(sent.at(-1).chat_id, 7);
  await hook(e, { message: { chat: { id: 42 }, from: { id: 42 }, text: "/reply 7 Реквизиты: ..." } });
  assert.ok(sent.some((m) => m.chat_id === "7" && m.text === "Реквизиты: ..."));
});

test("счёт на оплату — в звёздах Telegram по цене тарифа", async () => {
  const e = env();
  sent.length = 0;
  await hook(e, { message: { chat: { id: 7 }, from: { id: 7 }, text: "/buy_family" } });
  const inv = sent.find((m) => m.method === "sendInvoice");
  assert.equal(inv.currency, "XTR");
  assert.equal(inv.prices[0].amount, 120);
  assert.equal(inv.provider_token, undefined);
});
