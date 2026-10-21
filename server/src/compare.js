// «Твой ПК против таких же»: приём анонимных снимков и сводка по похожим ПК.
//
// Хранилище — D1 (binding COMPARE_DB), схема в server/compare-schema.sql.
// В базе нет ни IP, ни ключа подписки: только случайный id установки, железо,
// замеры и id программ из открытой базы known.json. Наружу отдаём только сводку
// (медианы, перцентили, доли программ) и только если похожих ПК не меньше MIN_PEERS.

export const MIN_PEERS = 5;
const MAX_ROWS = 3000;              // сколько похожих ПК читать за раз
const FRESH_DAYS = 180;             // старые снимки в сравнении не участвуют
const RESUBMIT_SECONDS = 10 * 60;   // одна установка — не чаще раза в 10 минут

export const METRICS = {
  boot_s: [0, 3600],
  idle_cpu: [0, 100],
  ram_used_pct: [0, 100],
  procs: [0, 5000],
  startup: [0, 1000],
  junk_gb: [0, 10000],
};

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

const str = (v, max = 60) => (typeof v === "string" ? v.toLowerCase().trim().slice(0, max) : "");
const int = (v, lo, hi) => (Number.isInteger(v) && v >= lo && v <= hi ? v : 0);

// Проверяет и обрезает снимок. Всё лишнее отбрасывается, а не сохраняется.
export function clean(body) {
  if (!body || typeof body !== "object") return null;
  const id = typeof body.id === "string" && /^[0-9a-f]{32}$/.test(body.id) ? body.id : null;
  if (!id || body.schema !== 1) return null;
  const hw = body.hw && typeof body.hw === "object" ? body.hw : {};
  const m = body.metrics && typeof body.metrics === "object" ? body.metrics : {};
  const metrics = {};
  for (const [k, [lo, hi]] of Object.entries(METRICS)) {
    const v = m[k];
    metrics[k] = typeof v === "number" && Number.isFinite(v) && v >= lo && v <= hi ? v : null;
  }
  const programs = Array.isArray(body.programs)
    ? [...new Set(body.programs.filter((p) => typeof p === "string" && /^[a-z0-9_.-]{1,64}$/i.test(p)))].slice(0, 200)
    : [];
  const os = str(hw.os, 16);
  return {
    id,
    os: /^(win\d+|mac\d*|linux)$/.test(os) ? os : "",
    cpu: str(hw.cpu),
    cores: int(hw.cores, 1, 512),
    threads: int(hw.threads, 1, 1024),
    ram_gb: int(hw.ram_gb, 1, 4096),
    disk: ["ssd", "hdd"].includes(hw.disk) ? hw.disk : "",
    gpu: str(hw.gpu),
    metrics,
    programs,
    unknown: int(body.unknown_programs, 0, 1000),
  };
}

function quantile(sorted, q) {
  if (!sorted.length) return null;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  const v = sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  return Math.round(v * 10) / 10;
}

// Сводка по похожим ПК для конкретного снимка. Чистая функция — её проверяют тесты.
export function summarize(mine, rows, level) {
  const peers = rows.filter((r) => r.id !== mine.id);
  if (peers.length < MIN_PEERS) return { peers: peers.length, level, enough: false };
  const metrics = {};
  for (const k of Object.keys(METRICS)) {
    const vals = peers.map((r) => r.metrics[k]).filter((v) => typeof v === "number").sort((a, b) => a - b);
    if (vals.length >= MIN_PEERS) {
      metrics[k] = { median: quantile(vals, 0.5), p25: quantile(vals, 0.25), p75: quantile(vals, 0.75), n: vals.length };
    }
  }
  // «Быстрые» — лучшая половина по среднему месту в загрузке, простое и занятой памяти
  const keys = ["boot_s", "idle_cpu", "ram_used_pct"].filter((k) => metrics[k]);
  const score = (r) => {
    const ranks = keys
      .filter((k) => typeof r.metrics[k] === "number")
      .map((k) => peers.filter((p) => typeof p.metrics[k] === "number" && p.metrics[k] < r.metrics[k]).length / metrics[k].n);
    return ranks.length ? ranks.reduce((a, b) => a + b, 0) / ranks.length : 0.5;
  };
  const scored = peers.map((r) => [score(r), r]).sort((a, b) => a[0] - b[0]);
  const fast = scored.slice(0, Math.max(Math.ceil(scored.length / 2), MIN_PEERS)).map(([, r]) => r);
  // Доли — только для программ из снимка самого спрашивающего: чужие списки не раскрываем
  const programs = {};
  const share = (list, p) => Math.round((list.filter((r) => r.programs.includes(p)).length / list.length) * 100) / 100;
  for (const p of mine.programs) programs[p] = { all: share(peers, p), fast: share(fast, p) };
  return { peers: peers.length, level, enough: true, metrics, programs };
}

const LEVELS = [
  ["model", "cpu = ?1 AND ram_gb = ?2 AND disk = ?3", (s) => [s.cpu, s.ram_gb, s.disk], (s) => s.cpu],
  ["class", "cores = ?1 AND threads = ?2 AND ram_gb = ?3 AND disk = ?4", (s) => [s.cores, s.threads, s.ram_gb, s.disk], (s) => s.cores],
  ["broad", "ram_gb = ?1 AND disk = ?2", (s) => [s.ram_gb, s.disk], () => true],
];

async function peersFor(env, s) {
  const since = Math.floor(Date.now() / 1000) - FRESH_DAYS * 86400;
  let last = { peers: 0, level: "broad", enough: false };
  for (const [level, where, args, usable] of LEVELS) {
    if (!usable(s)) continue;
    const params = args(s);
    const { results } = await env.COMPARE_DB.prepare(
      `SELECT id, metrics, programs FROM snapshots WHERE ${where} AND updated >= ?${params.length + 1} ` +
      `ORDER BY updated DESC LIMIT ${MAX_ROWS}`,
    ).bind(...params, since).all();
    const rows = results.map((r) => ({ id: r.id, metrics: JSON.parse(r.metrics), programs: JSON.parse(r.programs) }));
    last = summarize(s, rows, level);
    if (last.enough) return last;
  }
  return last;
}

// POST /compare — сохранить (или заменить) свой снимок и получить сводку. Нужна подписка.
export async function handleCompare(request, env, lic) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const s = clean(body);
  if (!s) return json({ error: "bad snapshot" }, 400);
  const now = Math.floor(Date.now() / 1000);
  const prev = await env.COMPARE_DB.prepare("SELECT updated FROM snapshots WHERE id = ?1").bind(s.id).first();
  if (prev && now - prev.updated < RESUBMIT_SECONDS) {
    return json(await peersFor(env, s)); // сводку отдадим, но снимок не перезаписываем
  }
  await env.COMPARE_DB.prepare(
    "INSERT OR REPLACE INTO snapshots (id, updated, os, cpu, cores, threads, ram_gb, disk, gpu, metrics, programs, unknown) " +
    "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
  ).bind(s.id, now, s.os, s.cpu, s.cores, s.threads, s.ram_gb, s.disk, s.gpu,
    JSON.stringify(s.metrics), JSON.stringify(s.programs), s.unknown).run();
  return json(await peersFor(env, s));
}

// POST /compare/forget — удалить свой снимок. Подписка не нужна: id знает только сама установка.
export async function handleCompareForget(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const id = body && typeof body.id === "string" && /^[0-9a-f]{32}$/.test(body.id) ? body.id : null;
  if (!id) return json({ error: "bad id" }, 400);
  await env.COMPARE_DB.prepare("DELETE FROM snapshots WHERE id = ?1").bind(id).run();
  return json({ ok: true });
}
