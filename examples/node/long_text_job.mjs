#!/usr/bin/env node
/**
 * Node 版异步任务：提交 → 轮询 → 下载。适合几千字、必定超过网关超时的稿子。
 *
 *   node examples/node/long_text_job.mjs --file 有声书一章.txt -o 成品.mp3
 *   node examples/node/long_text_job.mjs "短句也行" --interval 2
 *
 * 前提：服务端开 ADAPTER_JOBS_ENABLED=1，否则提交直接 404。
 * 没开也能跑长稿 —— 用 python/examples 里的 batch.py 分段，或自己按 2000 字切。
 */
import { readFile } from 'node:fs/promises';
import process from 'node:process';

import {
  die, explain, headers, human, loadEnv, normalizeBase, parseArgs, saveAudio,
} from './_shared.mjs';

const HELP = `用法: node long_text_job.mjs "长文本" [选项]

  -f, --file <路径>      从文件读文本（推荐，几千字用这个）
  -o, --out <路径>       输出文件，默认 job.mp3
      --format <fmt>     mp3|opus|wav|pcm
      --model <name>     引擎，默认 eleven_v3
      --voice <name>     音色，默认 yun
      --interval <sec>   轮询间隔，默认 3
      --max-wait <sec>   最长等待，默认 900
  -b, --base-url <url>   适配器地址，含 /v1
  -k, --token <token>    令牌
  -q, --quiet            只输出错误`;

async function call(base, token, method, path, payload, timeout) {
  const res = await fetch(base + path, {
    method,
    headers: headers(token),
    body: payload === undefined ? undefined : JSON.stringify(payload),
    signal: AbortSignal.timeout(timeout * 1000),
  });
  return { res, buf: Buffer.from(await res.arrayBuffer()) };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  loadEnv();
  const o = parseArgs(process.argv.slice(2), { out: 'job.mp3', interval: 3, maxWait: 900 });
  if (o.help) {
    console.log(HELP);
    return 0;
  }

  let text = o.text.join(' ').trim();
  if (o.file) {
    text = await readFile(o.file, 'utf8');
  } else if (!text && !process.stdin.isTTY) {
    const chunks = [];
    for await (const c of process.stdin) chunks.push(c);
    text = Buffer.concat(chunks).toString('utf8');
  }
  if (!text.trim()) die('没有要合成的文本。例：node long_text_job.mjs --file 稿子.txt');

  const base = normalizeBase(o.baseUrl);
  const payload = { model: o.model, voice: o.voice, input: text, response_format: o.format };
  if (o.speed !== null && !Number.isNaN(o.speed)) payload.speed = o.speed;
  if (o.textClean) payload.text_clean = o.textClean;

  console.log(`→ 提交任务，${text.length} 字符`);
  const sub = await call(base, o.token, 'POST', '/audio/speech/jobs', payload, o.timeout);
  if (sub.res.status === 404) {
    die(`异步接口没开（HTTP 404）\n  ${explain(404, sub.buf.toString('utf8'))}
  → 服务端加 ADAPTER_JOBS_ENABLED=1 后重启；不想改配置就分段提交同步接口`);
  }
  if (sub.res.status !== 202) {
    die(`提交失败 HTTP ${sub.res.status}\n  ${explain(sub.res.status, sub.buf.toString('utf8'))}`, 1);
  }

  const job = JSON.parse(sub.buf.toString('utf8'));
  console.log(`  job_id=${job.id}  预估 ${job.estimated_seconds}s  队列位置 ${job.queue_position}`);

  // 轮询到终态。等待期间不做重提 —— 重提只会让队列里多一份同样的活
  const deadline = Date.now() + o.maxWait * 1000;
  let state = null;
  let jobView = null;
  while (Date.now() < deadline) {
    const { res, buf } = await call(base, o.token, 'GET', `/audio/speech/jobs/${job.id}`, undefined, o.timeout);
    if (res.status === 404) {
      die(`job 查不到（404）\n  → 服务端是否重启过？任务存在内存里，重启即丢`);
    }
    if (res.status !== 200) die(`轮询失败 HTTP ${res.status}\n  ${explain(res.status, buf.toString('utf8'))}`);
    jobView = JSON.parse(buf.toString('utf8'));
    if (jobView.status !== state) {
      console.log(`  [${new Date().toLocaleTimeString()}] ${jobView.status}`);
      state = jobView.status;
    }
    if (['succeeded', 'failed', 'cancelled'].includes(state)) break;
    await sleep(o.interval * 1000);
  }

  if (state !== 'succeeded') {
    die(`任务未成功：${(jobView && jobView.error) || state || '等待超时'}
  ${(jobView && jobView.fix) || `手动查：curl ${base}/audio/speech/jobs/${job.id}`}`);
  }

  console.log(`  ✓ 合成完成：${human(jobView.audio_bytes || 0)}，${jobView.duration_sec}s，`
    + `计费 ${jobView.usage_chars} 字符`);

  const dl = await call(base, o.token, 'GET', `/audio/speech/jobs/${job.id}/audio`, undefined, o.timeout);
  if (dl.res.status !== 200) {
    die(`下载失败 HTTP ${dl.res.status}\n  ${explain(dl.res.status, dl.buf.toString('utf8'))}`);
  }
  await saveAudio(dl.buf, dl.res, o.out);
  console.log(`→ 已保存 ${o.out}（${human(dl.buf.length)}）`);
  console.log(`\n提示：结果在服务端只保留 ${jobView.expires_in} 秒，过期后 job 与音频一起清掉 —— 先下载再离开。`);
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((e) => die(String(e && e.stack ? e.stack : e), 1));
