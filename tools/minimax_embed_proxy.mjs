#!/usr/bin/env node
/**
 * MiniMax Embeddings → OpenAI 兼容格式 转换代理
 *
 * 为什么需要这个：
 *   OpenClaw 的 memorySearch（provider=openai）固定用 OpenAI 协议：
 *     请求  POST {baseUrl}/embeddings  {model, input: string[]}
 *     响应  {data: [{embedding: number[]}]}
 *
 *   MiniMax 的 /v1/embeddings 协议不同：
 *     请求  {model, texts: string[], type: "db"|"query"}
 *     响应  {vectors: number[][], base_resp:{status_code,status_msg}}
 *
 *   两者不兼容 → 本代理负责双向翻译，让 OpenClaw 用现成的 MiniMax key
 *   就能拿到中文 embeddings，无需额外申请 OpenAI/Gemini 凭证。
 *
 * type 映射（MiniMax 官方要求）：
 *   index（建库）→ db
 *   query（检索）→ query
 *   OpenClaw 走同一个 endpoint，代理通过请求头 X-Memory-Op 区分；
 *   未指定时默认 db（建库优先，且 query 向量同样可用）。
 *
 * 用法：
 *   node minimax_embed_proxy.mjs            # 监听 127.0.0.1:8799
 *   PORT=8800 node minimax_embed_proxy.mjs
 */

import http from "node:http";
import { readFileSync } from "node:fs";

const PORT = Number(process.env.PORT || 8799);
const HOST = process.env.HOST || "127.0.0.1";
const MINIMAX_URL = process.env.MINIMAX_URL || "https://api.minimaxi.com/v1/embeddings";
const MINIMAX_MODEL = process.env.MINIMAX_MODEL || "embo-01";
const CONFIG_PATH = process.env.OPENCLAW_CONFIG || "/root/.openclaw/openclaw.json";

function loadApiKey() {
  // 优先环境变量，其次从 openclaw.json 的 minimax provider 读取
  if (process.env.MINIMAX_API_KEY) return process.env.MINIMAX_API_KEY;
  try {
    const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
    const key = cfg?.models?.providers?.minimax?.apiKey;
    if (key) return key;
  } catch {
    /* ignore */
  }
  return null;
}

const API_KEY = loadApiKey();
if (!API_KEY) {
  console.error("[minimax-embed-proxy] FATAL: 找不到 MiniMax API key");
  process.exit(1);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
      if (data.length > 10 * 1024 * 1024) reject(new Error("body too large"));
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
    });
    res.end(body);
  };

  if (req.method === "GET" && req.url === "/health") {
    return send(200, { ok: true, model: MINIMAX_MODEL });
  }
  // OpenClaw 可能用 baseUrl + "/embeddings"（如 http://host/v1/embeddings）
  // 因此同时接受 /embeddings 和 /v1/embeddings
  const path = (req.url || "").split("?")[0];
  if (req.method !== "POST" || !/(^|\/)v?1\/embeddings$|^\/embeddings$/.test(path)) {
    return send(404, { error: { message: "not found", type: "invalid_request_error" } });
  }

  try {
    const raw = await readBody(req);
    const parsed = JSON.parse(raw || "{}");

    // OpenClaw 发 {model, input: string[]}；也兼容直接传 texts
    let texts = Array.isArray(parsed.input) ? parsed.input : parsed.texts;
    if (typeof texts === "string") texts = [texts];
    if (!Array.isArray(texts) || texts.length === 0) {
      return send(400, {
        error: { message: "input must be a non-empty array of strings", type: "invalid_request_error" },
      });
    }

    // MiniMax 的 type 会切换「向量空间」：db 与 query 是两个不同空间
    //   （同一文本在两个空间下余弦相似度仅 ~0.74，不是同一空间）
    // OpenClaw 的索引与检索都走这一个 endpoint，因此必须用**同一个 type**，
    // 否则查询向量与库内向量不同空间，排序被系统性稀释（实测 0.869 → 0.814）。
    // 这里统一用 db。
    const mmType = "db";
    const op = "db";

    if (process.env.PROXY_DEBUG) {
      console.log(
        `[minimax-embed-proxy] path=${path} n=${texts.length} op=${op} mmType=${mmType} model=${parsed.model || MINIMAX_MODEL} first="${String(texts[0]).slice(0, 40)}"`
      );
    }

    const mmRes = await fetch(MINIMAX_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${API_KEY}`,
      },
      body: JSON.stringify({
        model: parsed.model || MINIMAX_MODEL,
        texts,
        type: mmType,
      }),
    });

    const mmJson = await mmRes.json();
    const br = mmJson?.base_resp || {};
    if (br.status_code !== 0 || !mmJson?.vectors) {
      console.error("[minimax-embed-proxy] upstream error:", JSON.stringify(br));
      return send(502, {
        error: {
          message: `minimax embeddings failed: ${br.status_msg || mmRes.status}`,
          type: "upstream_error",
        },
      });
    }

    // MiniMax {vectors: number[][]} → OpenAI {data:[{embedding:number[]}]}
    const data = mmJson.vectors.map((embedding, index) => ({
      embedding,
      index,
      object: "embedding",
    }));

    return send(200, {
      object: "list",
      data,
      model: parsed.model || MINIMAX_MODEL,
      usage: { prompt_tokens: 0, total_tokens: 0 },
    });
  } catch (err) {
    console.error("[minimax-embed-proxy] error:", err?.message || err);
    return send(500, {
      error: { message: String(err?.message || err), type: "proxy_error" },
    });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[minimax-embed-proxy] listening on http://${HOST}:${PORT}`);
  console.log(`[minimax-embed-proxy] upstream=${MINIMAX_URL} model=${MINIMAX_MODEL}`);
});
