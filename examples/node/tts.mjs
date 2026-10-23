#!/usr/bin/env node
/**
 * Node 版最小调用（Node 18+，用内置 fetch，不需要 npm install）。
 *
 *   node examples/node/tts.mjs "你好，世界"
 *   node examples/node/tts.mjs --file 稿子.txt -o 成品.mp3 --speed 0.9
 *   echo "管道进来也行" | node examples/node/tts.mjs
 *
 * 想用 npm 上的 openai 包（npm i openai），写法几乎一样，见 examples/README.md。
 */
import { readFile } from 'node:fs/promises';
import process from 'node:process';

import {
  FORMATS, die, explain, headers, human, loadEnv, normalizeBase, parseArgs,
  saveAudio, showTrace,
} from './_shared.mjs';

const HELP = `用法: node tts.mjs "文本" [选项]

  -f, --file <路径>     从文件读文本（UTF-8）
  -o, --out <路径>      输出文件，默认 out.mp3
      --format <fmt>    mp3|opus|wav|pcm，默认 mp3
      --speed <n>       0.25–4.0，服务端钳制到 0.7–1.2
      --model <name>    引擎，默认 eleven_v3
      --voice <name>    音色，默认 yun
      --text-clean <m>  off|safe|normalize
  -b, --base-url <url>  适配器地址，含 /v1
  -k, --token <token>   令牌（或环境变量 ADAPTER_TOKEN）
      --timeout <sec>   客户端超时，默认 180
  -q, --quiet           只输出错误`;

async function main() {
  loadEnv();
  const o = parseArgs(process.argv.slice(2));
  if (o.help) {
    console.log(HELP);
    return 0;
  }
  if (!FORMATS.includes(o.format)) die(`--format 只能是 ${FORMATS.join('/')}`);

  let text = o.text.join(' ').trim();
  if (o.file) {
    text = await readFile(o.file, 'utf8');
  } else if (!text && !process.stdin.isTTY) {
    const chunks = [];
    for await (const c of process.stdin) chunks.push(c);
    text = Buffer.concat(chunks).toString('utf8');
  }
  if (!text.trim()) die('没有要合成的文本。例：node tts.mjs "你好，世界"');

  const base = normalizeBase(o.baseUrl);
  const payload = { model: o.model, input: text, response_format: o.format };
  if (o.voice) payload.voice = o.voice;
  if (o.speed !== null && !Number.isNaN(o.speed)) payload.speed = o.speed;
  if (o.textClean) payload.text_clean = o.textClean;

  if (!o.quiet) {
    console.log(`→ POST ${base}/audio/speech`);
    console.log(`  model=${o.model} voice=${o.voice || '(模型自带)'} format=${o.format} chars=${text.length}`);
  }

  const t0 = Date.now();
  let res;
  try {
    res = await fetch(`${base}/audio/speech`, {
      method: 'POST',
      headers: headers(o.token),
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(o.timeout * 1000),
    });
  } catch (e) {
    // fetch 的网络错误只有一句 "fetch failed"，把「下一步查什么」补上
    die(`连不上 ${base}（${e.name}: ${e.message}）
  1) 服务起来了吗： curl -s http://127.0.0.1:8123/healthz
  2) 容器在跑吗：   docker ps --filter name=elevenlabs-adapter
  3) 地址填对了吗： base 必须含 /v1`);
  }

  const buf = Buffer.from(await res.arrayBuffer());
  const elapsed = (Date.now() - t0) / 1000;

  if (!res.ok) die(`HTTP ${res.status}\n  ${explain(res.status, buf.toString('utf8'))}`, 1);

  await saveAudio(buf, res, o.out);
  if (!o.quiet) showTrace(res, elapsed, buf.length, o.out);
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((e) => die(String(e && e.stack ? e.stack : e), 1));
