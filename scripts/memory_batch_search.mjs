/**
 * 批量记忆检索 —— 一个 Node 进程跑完所有查询。
 *
 * 2026-10-29 新增（性能）:
 *   `openclaw memory search` 每次都要起一个 Node 进程
 *   （本机 2 核实测 **~4.6s/次**；其中 embedding 只要 0.36s，
 *    其余全是 Node 启动 + 插件注册）。
 *
 *   `verify_l1_retrieval.py` 要做 17 次真实检索 → 17 × 4.6s ≈ **78 秒**，
 *   占整个 healthcheck 的 80%，并让 healthcheck 逼近 600s 超时（【U】组 WARN）。
 *
 *   实测**并行更慢**（4 路并行 86.9s vs 串行 78.4s）—— 2 核机上并发只抢 CPU。
 *   → 正确做法是**一次启动，复用同一个 manager**，串行跑完所有查询。
 *
 * ⚠️ 三个坑（都实测踩过）:
 *   1. 打包后 export 名被压成单字母（search-manager 导出 n/r/t，
 *      其中 `n` 才是 getMemorySearchManager）→ 不能靠名字猜。
 *   2. `new URL('file://' + absPath)` 会把绝对路径当 host → 用 pathToFileUrl()。
 *   3. **导入 dist 下所有 .js 会触发 CLI 副作用**（"Memory index updated"）
 *      甚至 dump core → 只导入明确需要的模块。
 *
 * ⚠️ 必须与 CLI 结果**等价**，否则断言会装死。
 *    自检: `node scripts/memory_batch_search.mjs --self-test`
 */

import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(import.meta.url);

function pathToFileUrl(p) {
  // ⚠️ 不能用 `new URL('file://' + p)` —— 绝对路径会被当成 host
  return new URL('file://' + path.resolve(p).replace(/^\/+/, '/')).href;
}

function resolveDist() {
  const fs = require('fs');
  const bases = ['/root/.local/share/pnpm/global/5/.pnpm'];
  for (const base of bases) {
    try {
      for (const d of fs.readdirSync(base).filter((x) => x.startsWith('openclaw@'))) {
        const p = path.join(base, d, 'node_modules', 'openclaw', 'dist');
        if (fs.existsSync(p)) return p;
      }
    } catch { /* ignore */ }
  }
  throw new Error('cannot locate openclaw dist');
}

/** 在 dist 里按文件名关键字找模块（只扫匹配的，别全扫） */
async function importMatching(dist, pattern) {
  const fs = require('fs');
  const files = fs.readdirSync(dist).filter((f) => f.endsWith('.js') && pattern.test(f));
  const mods = [];
  for (const f of files) {
    try {
      mods.push(await import(pathToFileUrl(path.join(dist, f))));
    } catch { /* ignore */ }
  }
  return mods;
}

/**
 * 开一个 memory search manager。
 * 返回 { manager, cfg }；manager 由调用方负责 close()。
 */
async function openManager(dist) {
  const cfgMods = await importMatching(dist, /^config-.*\.js$/);
  const cfgMod = cfgMods.find((m) => typeof m?.loadConfig === 'function');
  const loadConfig = cfgMod?.loadConfig;
  if (!loadConfig) throw new Error('cannot find loadConfig');
  const cfg = await loadConfig();

  const smMods = await importMatching(dist, /search-manager-.*\.js$/);
  let lastErr;
  for (const m of smMods) {
    for (const k of Object.keys(m)) {
      const fn = m[k];
      if (typeof fn !== 'function') continue;
      for (const agentId of ['main', 'default']) {
        try {
          const r = await fn({ cfg, agentId });
          if (r && r.manager && typeof r.manager.search === 'function') {
            return { manager: r.manager, cfg };
          }
        } catch (e) { lastErr = e; }
      }
    }
  }
  throw new Error('cannot open memory search manager: ' + (lastErr?.message ?? 'unknown'));
}

/** 跑一批查询 → { query: [ {path, score, ...} ] } */
async function batchSearch(queries, maxResults = 6) {
  const dist = resolveDist();
  const { manager } = await openManager(dist);
  const out = {};
  try {
    for (const q of queries) {
      try {
        const results = await manager.search(q, { maxResults });
        out[q] = (results || []).map((r) => ({
          path: r.path,
          score: r.score,
          startLine: r.startLine,
          endLine: r.endLine,
          snippet: r.snippet,
        }));
      } catch (e) {
        out[q] = { error: String(e && e.message ? e.message : e) };
      }
    }
  } finally {
    await manager.close?.().catch(() => {});
  }
  return out;
}

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => (data += c));
    process.stdin.on('end', () => resolve(data));
  });
}

async function selfTest() {
  const { execFile } = require('node:child_process');
  const { promisify } = require('node:util');
  const exec = promisify(execFile);
  const q = '测试记忆检索等价性';

  const batch = await batchSearch([q]);
  let cli = [];
  try {
    const { stdout } = await exec('openclaw', ['memory', 'search', q, '--json'], {
      timeout: 120000,
      env: { ...process.env, OPENCLAW_LOG_LEVEL: 'warn' },
    });
    const i = stdout.indexOf('{');
    cli = (i >= 0 ? JSON.parse(stdout.slice(i)) : {}).results || [];
  } catch (e) {
    console.log('FAIL CLI 调用失败:', String(e.message).slice(0, 120));
    process.exit(1);
  }

  const b = batch[q] || [];
  if (!Array.isArray(b)) {
    console.log('FAIL batch 返回不是数组');
    process.exit(1);
  }
  if (b.length !== cli.length) {
    console.log(`FAIL 数量不一致: batch=${b.length} cli=${cli.length}`);
    process.exit(1);
  }
  for (let i = 0; i < cli.length; i++) {
    if (b[i].path !== cli[i].path) {
      console.log(`FAIL 第${i}条 path 不一致: batch=${b[i].path} cli=${cli[i].path}`);
      process.exit(1);
    }
    if (Math.abs((b[i].score ?? 0) - (cli[i].score ?? 0)) > 1e-6) {
      console.log(`FAIL 第${i}条 score 不一致: batch=${b[i].score} cli=${cli[i].score}`);
      process.exit(1);
    }
  }
  console.log(`PASS batch 与 CLI 一致（${b.length} 条）`);
}

async function main() {
  if (process.argv.includes('--self-test')) {
    await selfTest();
    return;
  }
  const raw = await readStdin();
  const input = JSON.parse(raw || '{}');
  const queries = input.queries || [];
  const maxResults = input.maxResults ?? 6;
  const out = await batchSearch(queries, maxResults);
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  console.error('FATAL', e && e.stack ? e.stack : e);
  process.exit(1);
});
