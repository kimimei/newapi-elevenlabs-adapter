/**
 * Node 示例共用的小工具：读 .env、规整地址、解析参数、翻译错误。
 * 零依赖 —— Node 18+ 自带 fetch 就够跑通，不用为了看个示例先 npm install。
 */
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

export const FORMATS = ['mp3', 'opus', 'wav', 'pcm'];

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ENV_FILE = path.join(HERE, '..', '.env');

export function loadEnv() {
  if (!existsSync(ENV_FILE)) return;
  // 同步读即可：必须在任何默认值求值前完成，且文件只有几十行
  for (const line of readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
    const s = line.trim();
    if (!s || s.startsWith('#') || !s.includes('=')) continue;
    const i = s.indexOf('=');
    const k = s.slice(0, i).trim();
    const v = s.slice(i + 1).trim().replace(/^["']|["']$/g, '');
    if (k && process.env[k] === undefined) process.env[k] = v;
  }
}

/** 补全 /v1 并提示 —— 静默修补会让人以为自己填对了。 */
export function normalizeBase(url) {
  let u = (url || process.env.ADAPTER_BASE_URL || 'http://127.0.0.1:8123/v1')
    .trim().replace(/\/+$/, '');
  if (u.endsWith('/audio/speech')) u = u.slice(0, -'/audio/speech'.length);
  if (!u.endsWith('/v1')) {
    console.error(`· base_url 未以 /v1 结尾，已自动补全为 ${u}/v1`);
    u += '/v1';
  }
  return u;
}

export function die(msg, code = 2) {
  console.error(`\n✗ ${msg}`);
  process.exit(code);
}

const VALUE_FLAGS = new Set([
  '--file', '-f', '--out', '-o', '--format', '--speed', '--model', '--voice',
  '--text-clean', '--token', '-k', '--base-url', '-b', '--timeout',
  '--interval', '--max-wait', '--segment',
]);

/** 手写解析：参数少，引 yargs 反而让人以为这个示例有依赖。 */
export function parseArgs(argv, defaults = {}) {
  const o = {
    text: [], file: null, out: 'out.mp3', format: 'mp3', speed: null,
    model: process.env.ADAPTER_MODEL || 'eleven_v3',
    voice: process.env.ADAPTER_VOICE || 'yun',
    textClean: null, token: process.env.ADAPTER_TOKEN || '',
    baseUrl: process.env.ADAPTER_BASE_URL || '',
    timeout: Number(process.env.REQUEST_TIMEOUT || 180), quiet: false,
    ...defaults,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-h' || a === '--help') { o.help = true; continue; }
    if (a === '-q' || a === '--quiet') { o.quiet = true; continue; }
    if (a === '--no-concat') { o.concat = false; continue; }
    if (a === '--force') { o.force = true; continue; }
    if (VALUE_FLAGS.has(a)) {
      const v = argv[++i];
      switch (a) {
        case '--file': case '-f': o.file = v; break;
        case '--out': case '-o': o.out = v; break;
        case '--format': o.format = v; break;
        case '--speed': o.speed = Number(v); break;
        case '--model': o.model = v; break;
        case '--voice': o.voice = v; break;
        case '--text-clean': o.textClean = v; break;
        case '--token': case '-k': o.token = v; break;
        case '--base-url': case '-b': o.baseUrl = v; break;
        case '--timeout': o.timeout = Number(v); break;
        case '--interval': o.interval = Number(v); break;
        case '--max-wait': o.maxWait = Number(v); break;
        case '--segment': o.segment = Number(v); break;
        default: break;
      }
      continue;
    }
    if (a.startsWith('-')) die(`不认识的参数：${a}（--help 看用法）`);
    o.text.push(a);
  }
  return o;
}

export function headers(token) {
  return {
    'Content-Type': 'application/json; charset=utf-8',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

/** 状态码 → 下一步改哪里。优先用适配器给的 error.fix。 */
export function explain(status, body) {
  let detail = '';
  let fix = '';
  let structured = false;
  try {
    const obj = JSON.parse(body);
    const err = obj.error || {};
    detail = err.message || obj.message || '';
    fix = err.fix || '';
    structured = Boolean(err && Object.keys(err).length) || Boolean(obj.detail);
  } catch {
    detail = String(body).slice(0, 200);
  }
  const hints = {
    400: '请求参数不对：response_format 只支持 mp3/opus/wav/pcm',
    401: '令牌不对：直连自建实例填上游 Key，经中转站填面板令牌，两者不能互换',
    403: '上游拒绝：额度用完 / 无该模型权限',
    404: '路径或模型不存在：检查 base 是否多了或少了一个 /v1',
    409: '资源还没就绪（异步任务音频未生成完）',
    429: '限流：降低并发或稍后重试',
    501: '不支持流式：去掉 stream=true',
    502: '上游异常或超时：长文本改用异步任务或分段',
  };
  let hint = hints[status] || '看响应体原文';
  if (!structured && status >= 500) {
    // 适配器的错误体一定是 {"error":{...}}；不是这个结构说明响应来自
    // 反代 / 云网关 / 占了同一端口的别的服务 —— 先查地址，别去查上游
    hint = '这个响应不是适配器的错误结构，多半来自反代/网关或别的服务：'
      + '先确认端口上跑的是不是适配器（curl -s http://主机:端口/healthz）\n  ' + hint;
  } else if (fix) {
    // 适配器已经说清怎么改就不再叠通用猜测：两条并列时人会照着错的那条改
    hint = '';
  }
  return [detail || '(无响应体)', hint, fix && `→ ${fix}`].filter(Boolean).join('\n  ');
}

export function human(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/**
 * 按 Content-Type 判断再落盘。
 * 不判断的后果：失败时把 JSON 错误体写进 .mp3，播放器打不开，
 * 然后一路往「格式转换」方向排错 —— 白查两小时。
 */
export async function saveAudio(buf, res, out) {
  const ctype = (res.headers.get('content-type') || '').toLowerCase();
  if (!ctype.startsWith('audio/')) {
    die(`服务端返回的不是音频（Content-Type: ${ctype || '缺失'}）：\n${buf.toString('utf8').slice(0, 600)}`, 1);
  }
  await mkdir(path.dirname(path.resolve(out)), { recursive: true });
  await writeFile(out, buf);
}

export function showTrace(res, elapsed, size, out) {
  const h = (n) => res.headers.get(n) || '-';
  console.log('✓ 200');
  console.log(`  X-Request-Id        ${h('x-request-id')}`);
  console.log(`  计费字符 / 上游积分  ${h('x-usage-input-characters')} / ${h('x-usage-upstream-credits')}`);
  console.log(`  实际下发 model/voice ${h('x-elevenlabs-model')} / ${h('x-elevenlabs-voice')}`);
  console.log(`  耗时 / 体积          ${elapsed.toFixed(2)}s / ${human(size)}`);
  for (const k of ['x-speed-clamped', 'x-text-clean']) {
    if (h(k) !== '-') console.log(`  ${k}${' '.repeat(22 - k.length)}${h(k)}`);
  }
  console.log(`  已保存              ${out}`);
}
