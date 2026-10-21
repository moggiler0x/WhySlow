-- База для «Сравнения с похожими ПК» (Cloudflare D1, binding COMPARE_DB).
-- Создать: npx wrangler d1 execute whyslow-compare --remote --file=server/compare-schema.sql
CREATE TABLE IF NOT EXISTS snapshots (
  id       TEXT PRIMARY KEY,   -- случайный id установки, с ключом подписки не связан
  updated  INTEGER NOT NULL,   -- unix-время последней отправки
  os       TEXT,
  cpu      TEXT,
  cores    INTEGER,
  threads  INTEGER,
  ram_gb   INTEGER,
  disk     TEXT,
  gpu      TEXT,
  metrics  TEXT NOT NULL,      -- JSON: boot_s, idle_cpu, ram_used_pct, procs, startup, junk_gb
  programs TEXT NOT NULL,      -- JSON: id записей known.json
  unknown  INTEGER
);
CREATE INDEX IF NOT EXISTS by_model ON snapshots (cpu, ram_gb, disk, updated);
CREATE INDEX IF NOT EXISTS by_class ON snapshots (cores, threads, ram_gb, disk, updated);
CREATE INDEX IF NOT EXISTS by_broad ON snapshots (ram_gb, disk, updated);
