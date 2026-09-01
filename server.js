import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import cors from 'cors';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const configPath = path.join(__dirname, 'config.json');
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const app = express();
app.disable('x-powered-by');
app.set('json spaces', 0);
const PORT = Number(process.env.PORT || 3000);
const STORAGE_URL = (config.APIFILE_URL || 'https://apifile.netlify.app').replace(/\/$/, '');
const STORAGE_TOKEN = config.APIFILE_ADMIN_TOKEN;
const ADMIN_USER = config.ADMIN_USER || '1v99ByRaro';
const ADMIN_PASS = config.ADMIN_PASS || '199';
const SESSION_SECRET = config.SESSION_SECRET || crypto.createHash('sha256').update(`${ADMIN_USER}:${ADMIN_PASS}`).digest('hex');
const ROOT = '/suc3ss4da';
const CACHE_TTL_MS = 2500;
const CHUNK_SIZE_BYTES = 95000;
const MAX_CHUNKS = 10000;
const cache = new Map();

function cloneData(value) {
  if (value === null || value === undefined) return value;
  if (typeof value !== 'object') return value;
  try { return JSON.parse(JSON.stringify(value)); } catch { return value; }
}

function getCachedValue(key) {
  const entry = cache.get(key);
  if (!entry) return undefined;
  if (Date.now() > entry.expiresAt) {
    cache.delete(key);
    return undefined;
  }
  return cloneData(entry.value);
}

function setCachedValue(key, value, ttl = CACHE_TTL_MS) {
  cache.set(key, { value: cloneData(value), expiresAt: Date.now() + ttl });
  return cloneData(value);
}

export default app;

app.use(cors());
app.use(express.json({ limit: '200mb' }));
app.use(express.urlencoded({ extended: true, limit: '200mb' }));
app.use((req, res, next) => {
  res.setHeader('X-Backend', 'Suc3ss4da');
  res.setHeader('X-Chunk-Support', 'enabled');
  next();
});

function auth(req, res, next) {
  const token = req.get('Authorization');
  if (!isSessionTokenValid(token)) return res.status(403).json({ error: 'admin apenas' });
  next();
}

function createSessionToken() {
  const payload = Buffer.from(JSON.stringify({ sub: 'admin', exp: Date.now() + 30 * 24 * 60 * 60 * 1000 })).toString('base64url');
  const signature = crypto.createHmac('sha256', SESSION_SECRET).update(payload).digest('base64url');
  return `${payload}.${signature}`;
}

function isSessionTokenValid(value) {
  if (!value || typeof value !== 'string') return false;
  const [payload, signature] = value.split('.');
  if (!payload || !signature) return false;
  const expected = crypto.createHmac('sha256', SESSION_SECRET).update(payload).digest('base64url');
  if (signature.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) return false;
  try { return JSON.parse(Buffer.from(payload, 'base64url').toString()).exp > Date.now(); } catch { return false; }
}

function safeName(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.-]+$/.test(value);
}

function storagePath(name) {
  const normalized = String(name || '').replace(/^\/+/, '').split('/').filter(Boolean);
  if (!normalized.length) throw new Error('nome de arquivo inválido');
  const validSegments = normalized.map((segment) => {
    if (!safeName(segment)) throw new Error('nome de arquivo inválido');
    return segment;
  });
  return `${ROOT}/${validSegments.join('/')}`;
}

function normalizeStoragePath(filePath) {
  return String(filePath || '').replace(/^\/+/, '');
}

function bodyStoragePath(filePath) {
  const normalized = normalizeStoragePath(filePath);
  return normalized ? `/${normalized}` : '/';
}

async function ensureParentFolder(filePath) {
  const parent = filePath.includes('/') ? filePath.slice(0, filePath.lastIndexOf('/')) : ROOT;
  const parts = parent.split('/').filter(Boolean);
  let current = '';

  for (const part of parts) {
    current = `${current}/${part}`;
    try {
      await storageRequest(`${STORAGE_URL}/api/folders/`, {
        method: 'POST',
        body: JSON.stringify({ path: current })
      });
    } catch (error) {
      if (error.status !== 400 && error.status !== 409 && error.status !== 404) {
        throw error;
      }
    }
  }
}

function externalUrl(filePath) {
  const normalized = normalizeStoragePath(filePath);
  return `${STORAGE_URL}/api/files/${normalized.split('/').map(encodeURIComponent).join('/')}`;
}

async function storageRequest(url, options = {}) {
  if (!STORAGE_TOKEN) throw new Error('APIFILE_ADMIN_TOKEN não configurado');
  const headers = { Authorization: `Bearer ${STORAGE_TOKEN}`, ...(options.headers || {}) };
  if (options.body && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(url, { ...options, headers });
  const text = await response.text();
  let body = text;
  try { body = JSON.parse(text); } catch { }
  if (!response.ok) {
    const error = new Error(body?.error || `Storage API respondeu ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function ensureStorageRoot() {
  if (!STORAGE_TOKEN) return;
  try {
    await storageRequest(`${STORAGE_URL}/api/folders/${encodeURIComponent(ROOT.replace(/^\//, ''))}`, { method: 'GET' });
  } catch (error) {
    if (error.status === 404) {
      try {
        await storageRequest(`${STORAGE_URL}/api/folders/`, { method: 'POST', body: JSON.stringify({ path: ROOT }) });
      } catch (folderError) {
        if (folderError.status !== 400 && folderError.status !== 409) {
          throw folderError;
        }
      }
    }
  }
}

function unwrapStoragePayload(payload) {
  if (payload == null) return payload;
  if (typeof payload === 'string') return payload;
  if (typeof payload !== 'object') return payload;
  if (payload.data !== undefined) return unwrapStoragePayload(payload.data);
  if (payload.content !== undefined) return payload.content;
  if (payload.items !== undefined) return payload.items;
  return payload;
}

async function readJson(name, fallback) {
  const cached = getCachedValue(name);
  if (cached !== undefined) return cached;
  try {
    const body = await storageRequest(externalUrl(storagePath(name)));
    const payload = unwrapStoragePayload(body);
    let value = payload;
    if (typeof value === 'string') {
      try { value = JSON.parse(value); } catch { value = value; }
    }
    if (value === undefined || value === null) value = fallback;
    setCachedValue(name, value, CACHE_TTL_MS * 6);
    return cloneData(value);
  } catch (error) {
    if (error.status === 404) return cloneData(fallback);
    throw error;
  }
}

async function writeJson(name, value) {
  await ensureStorageRoot();
  const filePath = storagePath(name);
  const content = JSON.stringify(value, null, 2);
  const safeValue = cloneData(value);
  try {
    await storageRequest(externalUrl(filePath), { method: 'PUT', body: JSON.stringify({ content }), headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    if (error.status !== 404) throw error;
    await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(filePath), content }), headers: { 'Content-Type': 'application/json' } });
  }
  setCachedValue(name, safeValue, CACHE_TTL_MS * 6);
  return safeValue;
}

async function writeText(name, content) {
  if (typeof content !== 'string') content = String(content ?? '');
  await ensureStorageRoot();
  const filePath = storagePath(name);
  await ensureParentFolder(filePath);
  const contentSize = Buffer.byteLength(content, 'utf8');

  if (contentSize <= CHUNK_SIZE_BYTES) {
    try {
      await storageRequest(externalUrl(filePath), { method: 'PUT', body: JSON.stringify({ content }), headers: { 'Content-Type': 'application/json' } });
    } catch (error) {
      if (error.status !== 404) throw error;
      await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(filePath), content }), headers: { 'Content-Type': 'application/json' } });
    }
    setCachedValue(`text:${name}`, content, CACHE_TTL_MS * 6);
    return;
  }

  const parts = [];
  const numChunks = Math.ceil(content.length / CHUNK_SIZE_BYTES);
  if (numChunks > MAX_CHUNKS) throw new Error(`Arquivo muito grande (${numChunks} chunks > ${MAX_CHUNKS})`);

  for (let i = 0; i < numChunks; i++) {
    const start = i * CHUNK_SIZE_BYTES;
    const end = Math.min(start + CHUNK_SIZE_BYTES, content.length);
    const chunk = content.slice(start, end);
    const chunkName = `${name}.chunk${String(i).padStart(4, '0')}`;
    parts.push(chunkName);
    const chunkPath = storagePath(chunkName);
    await ensureParentFolder(chunkPath);

    let retries = 0;
    while (retries < 3) {
      try {
        await storageRequest(externalUrl(chunkPath), { method: 'PUT', body: JSON.stringify({ content: chunk }), headers: { 'Content-Type': 'application/json' } });
        break;
      } catch (error) {
        if (error.status === 404 && retries === 0) {
          try {
            await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(chunkPath), content: chunk }), headers: { 'Content-Type': 'application/json' } });
            break;
          } catch (e) {
            retries++;
            if (retries >= 3) throw e;
            await new Promise(r => setTimeout(r, 300 * retries));
          }
        } else {
          retries++;
          if (retries >= 3) throw error;
          await new Promise(r => setTimeout(r, 300 * retries));
        }
      }
    }
  }

  const metaFilePath = storagePath(`${name}.meta.json`);
  const metaContent = JSON.stringify({ parts, totalChunks: numChunks, createdAt: new Date().toISOString() }, null, 2);
  try {
    await storageRequest(externalUrl(metaFilePath), { method: 'PUT', body: JSON.stringify({ content: metaContent }), headers: { 'Content-Type': 'application/json' } });
  } catch (error) {
    if (error.status !== 404) throw error;
    await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(metaFilePath), content: metaContent }), headers: { 'Content-Type': 'application/json' } });
  }

  try {
    await storageRequest(externalUrl(filePath), { method: 'DELETE' }).catch(() => {});
  } catch { }
  setCachedValue(`text:${name}`, content, CACHE_TTL_MS * 6);
}

async function readText(name) {
  const cachedText = cache.get(`text:${name}`);
  if (cachedText && Date.now() < cachedText.expiresAt) return cachedText.value;

  try {
    const metaBody = await storageRequest(externalUrl(storagePath(`${name}.meta.json`)));
    const metaPayload = unwrapStoragePayload(metaBody);
    let meta;
    if (typeof metaPayload === 'string') {
      try { meta = JSON.parse(metaPayload); } catch { meta = null; }
    } else if (metaPayload && typeof metaPayload === 'object') {
      meta = metaPayload;
    }

    if (meta && Array.isArray(meta.parts) && meta.parts.length > 0) {
      const chunkTexts = await Promise.all(meta.parts.map(async (chunkName) => {
        try {
          const chunkBody = await storageRequest(externalUrl(storagePath(chunkName)));
          const chunkPayload = unwrapStoragePayload(chunkBody);
          return typeof chunkPayload === 'string' ? chunkPayload : (chunkPayload?.content || '');
        } catch (e) {
          console.error(`Falha ao ler chunk ${chunkName}:`, e.message);
          return '';
        }
      }));
      const text = chunkTexts.join('');
      cache.set(`text:${name}`, { value: text, expiresAt: Date.now() + CACHE_TTL_MS * 6 });
      return text;
    }
  } catch (error) {
    if (error.status !== 404) {
      console.error(`Falha ao ler metadados ${name}:`, error.message);
    }
  }

  try {
    const body = await storageRequest(externalUrl(storagePath(name)));
    const payload = unwrapStoragePayload(body);
    const text = typeof payload === 'string' ? payload : (payload?.content || '');
    cache.set(`text:${name}`, { value: text, expiresAt: Date.now() + CACHE_TTL_MS * 6 });
    return text;
  } catch (e) {
    console.error(`Falha ao ler arquivo ${name}:`, e.message);
    throw e;
  }
}

function now() { return new Date().toISOString(); }
function token() { return crypto.randomBytes(32).toString('hex'); }
function jsonError(res, error) { return res.status(error.status || 500).json({ error: error.message }); }
function requireName(req, res, next) { if (!safeName(req.params.id)) return res.status(404).end(); next(); }

app.get('/api/status', (_req, res) => res.json({ status: 'online', time: now() }));
app.post('/api/admin/login', (req, res) => {
  const { user = '', password = '' } = req.body || {};
  if (user.toLowerCase() !== ADMIN_USER.toLowerCase() || password !== ADMIN_PASS) return res.status(401).json({ error: 'Credenciais admin inválidas', admin: false });
  res.json({ token: createSessionToken(), user: 'admin', admin: true });
});
app.get('/api/admin/verify', auth, (_req, res) => res.json({ admin: true, valid: true }));
app.post('/api/logout', (_req, res) => res.json({ success: true }));

function normalizeArray(value, fallback = []) {
  if (Array.isArray(value)) return value;
  if (Array.isArray(value?.items)) return value.items;
  if (value && typeof value === 'object') {
    const entries = Object.values(value);
    return Array.isArray(entries) ? entries : fallback;
  }
  return fallback;
}

app.get('/api/logs', auth, async (_req, res) => { try { const logs = normalizeArray(await readJson('logs.json', []), []); res.json({ logs: logs.slice(-5000), pagination: { page: 1, limit: Math.min(logs.length, 5000), total: logs.length, pages: 1 } }); } catch (e) { jsonError(res, e); } });
app.get('/api/admin/banlist', auth, async (_req, res) => { try { const bans = normalizeArray(await readJson('bans.json', []), []); res.json(bans); } catch (e) { jsonError(res, e); } });
app.get('/api/produtos', auth, async (_req, res) => { try { const produtos = normalizeArray(await readJson('produtos.json', []), []); res.json(produtos); } catch (e) { jsonError(res, e); } });
app.get('/api/keys', auth, async (_req, res) => { try { const keys = await readJson('keys.json', {}); res.json(keys && typeof keys === 'object' && !Array.isArray(keys) ? keys : {}); } catch (e) { jsonError(res, e); } });
function durationSeconds(value) {
  if (value === undefined || value === null || value === '' || ['0', 'perm', 'permanent', 'infinite', 'indef'].includes(String(value).toLowerCase())) return null;
  if (/^\d+$/.test(String(value))) return Number(value);
  const match = String(value).toLowerCase().match(/^(\d+)([smhd])$/);
  if (!match) return null;
  return Number(match[1]) * ({ s: 1, m: 60, h: 3600, d: 86400 }[match[2]]);
}
function withExpiry(data, current = {}) {
  const seconds = durationSeconds(data.duration);
  return { ...current, owner: data.owner ?? current.owner ?? '', roblox: data.roblox ?? current.roblox ?? '', notes: data.notes ?? current.notes ?? '', active: data.active ?? current.active ?? true, ...(data.duration !== undefined ? { duration_seconds: seconds, expires_at: seconds === null ? null : Math.floor(Date.now() / 1000) + seconds } : {}) };
}
app.post('/api/keys', auth, async (req, res) => { try { const keys = await readJson('keys.json', {}); const key = token(); keys[key] = withExpiry(req.body || {}, { token: key, created: now(), uses: 0 }); await writeJson('keys.json', keys); res.json({ status: 'criada', key: keys[key] }); } catch (e) { jsonError(res, e); } });
app.get('/api/keys/:id', auth, requireName, async (req, res) => { try { const key = (await readJson('keys.json', {}))[req.params.id]; key ? res.json(key) : res.status(404).json({ error: 'key não encontrada' }); } catch (e) { jsonError(res, e); } });
app.put('/api/keys/:id', auth, requireName, async (req, res) => { try { const keys = await readJson('keys.json', {}); if (!keys[req.params.id]) return res.status(404).json({ error: 'key não encontrada' }); keys[req.params.id] = withExpiry(req.body || {}, keys[req.params.id]); await writeJson('keys.json', keys); res.json({ status: 'atualizada', key: keys[req.params.id] }); } catch (e) { jsonError(res, e); } });
app.post('/api/keys/:id/addtime', auth, requireName, async (req, res) => { try { const keys = await readJson('keys.json', {}); const key = keys[req.params.id]; const seconds = durationSeconds(req.body?.add); if (!key || seconds === null) return res.status(404).json({ error: 'key ou duração inválida' }); key.expires_at = Math.max(key.expires_at || Math.floor(Date.now() / 1000), Math.floor(Date.now() / 1000)) + seconds; key.duration_seconds = key.expires_at - Math.floor(Date.now() / 1000); await writeJson('keys.json', keys); res.json({ status: 'atualizado', key }); } catch (e) { jsonError(res, e); } });
app.delete('/api/keys/:id', auth, requireName, async (req, res) => { try { const keys = await readJson('keys.json', {}); if (!keys[req.params.id]) return res.status(404).json({ error: 'key não encontrada' }); delete keys[req.params.id]; await writeJson('keys.json', keys); res.json({ status: 'deletada' }); } catch (e) { jsonError(res, e); } });

function keyIsValid(key, keys) {
  const item = keys?.[key];
  return Boolean(item && item.active !== false && (!item.expires_at || Number(item.expires_at) >= Math.floor(Date.now() / 1000)));
}

app.get('/api/key/validate/:key', async (req, res) => {
  try {
    const keys = await readJson('keys.json', {});
    const item = keys[req.params.key];
    if (!keyIsValid(req.params.key, keys)) return res.status(403).json({ valid: false, reason: 'expired_or_inactive' });
    item.uses = (item.uses || 0) + 1;
    await writeJson('keys.json', keys);
    res.json({ valid: true, key: item });
  } catch (e) { jsonError(res, e); }
});

app.get('/api/stats', auth, async (_req, res) => {
  try {
    const [logs, produtos, keys, scripts, loader] = await Promise.all([
      readJson('logs.json', []),
      readJson('produtos.json', []),
      readJson('keys.json', {}),
      readJson('scripts.json', []),
      readJson('loader-index.json', [])
    ]);
    const normalizedLogs = normalizeArray(logs, []);
    const normalizedProdutos = normalizeArray(produtos, []);
    const normalizedScripts = normalizeArray(scripts, []);
    const normalizedLoader = normalizeArray(loader, []);
    const playerSet = new Set();
    for (const item of normalizedLogs) {
      if (item && item.id) playerSet.add(String(item.id));
    }
    res.json({
      players: playerSet.size,
      execucoes: normalizedLogs.length,
      scripts: normalizedLoader.length,
      raw_scripts: normalizedScripts.length,
      produtos: normalizedProdutos.length,
      keys: Object.keys(keys || {}).length
    });
  } catch (e) { jsonError(res, e); }
});
app.post('/api/admin/ban', auth, async (req, res) => { try { const bans = await readJson('bans.json', []); const entry = { ...req.body, data: new Date().toLocaleDateString('pt-BR'), hora: new Date().toLocaleTimeString('pt-BR') }; if (!bans.some((b) => ['hwid', 'ip', 'nick'].some((key) => entry[key] && b[key] === entry[key]))) await writeJson('bans.json', [...bans, entry]); res.json({ status: 'banido', entry }); } catch (e) { jsonError(res, e); } });
app.post('/api/admin/unban', auth, async (req, res) => { try { const { hwid, ip, nick } = req.body || {}; const bans = await readJson('bans.json', []); await writeJson('bans.json', bans.filter((b) => !((hwid && b.hwid === hwid) || (ip && b.ip === ip) || (nick && b.nick === nick)))); res.json({ status: 'desbanido' }); } catch (e) { jsonError(res, e); } });
app.get('/api/banlist', async (_req, res) => { try { const bans = normalizeArray(await readJson('bans.json', []), []); res.json(bans.map(({ hwid, ip, nick, id, data }) => ({ hwid, ip, nick, id, data }))); } catch (e) { jsonError(res, e); } });
app.post('/api/log', async (req, res) => {
  try {
    const data = req.body || {};
    const [logs, bans, keys] = await Promise.all([
      readJson('logs.json', []),
      readJson('bans.json', []),
      readJson('keys.json', {})
    ]);
    const normalizedLogs = normalizeArray(logs, []);
    const normalizedBans = normalizeArray(bans, []);
    const ip = req.headers['x-forwarded-for']?.split(',')[0]?.trim() || req.socket.remoteAddress;
    if (normalizedBans.some((ban) => (data.hwid && ban.hwid === data.hwid) || (data.nick && ban.nick === data.nick) || (ip && ban.ip === ip))) return res.status(403).json({ status: 'banido' });
    const key = req.get('X-Loader-Key') || req.query.key;
    if (key && !keyIsValid(key, keys)) return res.status(403).json({ error: 'key inválida ou expirada' });
    normalizedLogs.push({ ...data, ip, data: new Date().toLocaleDateString('pt-BR'), hora: new Date().toLocaleTimeString('pt-BR'), meta: { key: Boolean(key), user_agent: req.get('User-Agent') } });
    await writeJson('logs.json', normalizedLogs.slice(-5000));
    res.json({ status: 'registrado' });
  } catch (e) { jsonError(res, e); }
});

function collectionRoutes(collection, idKey = 'id') {
  app.post(`/api/${collection}`, auth, async (req, res) => { try { const list = normalizeArray(await readJson(`${collection}.json`, []), []); const item = { ...req.body, [idKey]: req.body[idKey] || crypto.randomUUID().replaceAll('-', ''), criado: now(), atualizado: now() }; await writeJson(`${collection}.json`, [...list, item]); res.json({ status: 'criado', [collection === 'scripts' ? 'script' : 'produto']: item }); } catch (e) { jsonError(res, e); } });
  app.get(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const list = normalizeArray(await readJson(`${collection}.json`, []), []); const item = list.find((value) => String(value[idKey]) === req.params.id); item ? res.json(item) : res.status(404).json({ error: 'não encontrado' }); } catch (e) { jsonError(res, e); } });
  app.put(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const list = normalizeArray(await readJson(`${collection}.json`, []), []); const index = list.findIndex((value) => String(value[idKey]) === req.params.id); if (index < 0) return res.status(404).json({ error: 'não encontrado' }); list[index] = { ...list[index], ...req.body, atualizado: now() }; await writeJson(`${collection}.json`, list); res.json({ status: 'atualizado', [collection === 'scripts' ? 'script' : 'produto']: list[index] }); } catch (e) { jsonError(res, e); } });
  app.delete(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const list = normalizeArray(await readJson(`${collection}.json`, []), []); const next = list.filter((value) => String(value[idKey]) !== req.params.id); if (next.length === list.length) return res.status(404).json({ error: 'não encontrado' }); await writeJson(`${collection}.json`, next); res.json({ status: 'deletado' }); } catch (e) { jsonError(res, e); } });
}
collectionRoutes('produtos');

async function getScriptEntry(id) {
  const list = normalizeArray(await readJson('scripts.json', []), []);
  const record = list.find((item) => String(item.id) === String(id));
  if (!record) return null;
  if (typeof record.codigo === 'string') return record;
  const fileCode = await readText(`scripts/${safeName(String(id))}.lua`).catch(() => '');
  return fileCode ? { ...record, codigo: fileCode } : record;
}

app.post('/api/scripts', auth, async (req, res) => {
  try {
    const list = normalizeArray(await readJson('scripts.json', []), []);
    const item = { ...req.body, id: req.body.id || crypto.randomUUID().replaceAll('-', ''), criado: now(), atualizado: now() };
    const codigo = typeof item.codigo === 'string' ? item.codigo : '';
    const stored = { ...item, codigo: undefined };
    await writeText(`scripts/${safeName(String(item.id))}.lua`, codigo);
    await writeJson('scripts.json', [...list.filter((value) => String(value.id) !== String(item.id)), stored]);
    res.json({ status: 'criado', script: { ...stored, codigo } });
  } catch (e) { jsonError(res, e); }
});
app.get('/api/scripts/:id', auth, requireName, async (req, res) => {
  try {
    const script = await getScriptEntry(req.params.id);
    script ? res.json(script) : res.status(404).json({ error: 'não encontrado' });
  } catch (e) { jsonError(res, e); }
});
app.put('/api/scripts/:id', auth, requireName, async (req, res) => {
  try {
    const list = normalizeArray(await readJson('scripts.json', []), []);
    const index = list.findIndex((value) => String(value.id) === req.params.id);
    if (index < 0) return res.status(404).json({ error: 'não encontrado' });
    const existing = list[index];
    const codigo = typeof req.body?.codigo === 'string' ? req.body.codigo : (await readText(`scripts/${safeName(String(req.params.id))}.lua`).catch(() => ''));
    const updated = { ...existing, ...req.body, id: req.params.id, atualizado: now() };
    await writeText(`scripts/${safeName(String(req.params.id))}.lua`, codigo);
    const stored = { ...updated, codigo: undefined };
    list[index] = stored;
    await writeJson('scripts.json', list);
    res.json({ status: 'atualizado', script: { ...stored, codigo } });
  } catch (e) { jsonError(res, e); }
});
app.delete('/api/scripts/:id', auth, requireName, async (req, res) => {
  try {
    const list = normalizeArray(await readJson('scripts.json', []), []);
    const next = list.filter((value) => String(value.id) !== req.params.id);
    if (next.length === list.length) return res.status(404).json({ error: 'não encontrado' });
    await writeText(`scripts/${safeName(String(req.params.id))}.lua`, '').catch(() => {});
    await writeJson('scripts.json', next);
    res.json({ status: 'deletado' });
  } catch (e) { jsonError(res, e); }
});
app.get('/api/scripts', auth, async (_req, res) => { try { const scripts = normalizeArray(await readJson('scripts.json', []), []); res.json(scripts.map(({ id, titulo, criado, atualizado }) => ({ id, titulo, criado, atualizado }))); } catch (e) { jsonError(res, e); } });

app.get('/api/raw/:id', async (req, res) => { if (!req.get('User-Agent')?.toLowerCase().includes('roblox') && !req.get('X-Roblox-UserId')) return res.status(403).send('403 Forbidden'); try { const script = await getScriptEntry(req.params.id); if (!script) return res.status(404).send('-- not found'); const codigo = typeof script.codigo === 'string' ? script.codigo : ''; res.type('text/plain').send(`local Maker = "Suc3ss4da"\nprint("by Suc3ss4da")\n\n${codigo}`); } catch (e) { jsonError(res, e); } });

app.get('/api/loader/list', auth, async (_req, res) => { try { res.json(normalizeArray(await readJson('loader-index.json', []), [])); } catch (e) { jsonError(res, e); } });
app.post('/api/loader/save', auth, async (req, res) => { try { const { id, content } = req.body || {}; if (!safeName(id) || content === undefined) return res.status(400).json({ error: 'missing id or content' }); await writeText(`loader-${id}.txt`, content); const list = normalizeArray(await readJson('loader-index.json', []), []); const item = { id, size: Buffer.byteLength(content), modified: new Date().toLocaleString('pt-BR') }; await writeJson('loader-index.json', [...list.filter((v) => v.id !== id), item]); res.json({ status: 'ok', path: id }); } catch (e) { jsonError(res, e); } });
app.post('/api/loader/delete', auth, async (req, res) => { try { const { id } = req.body || {}; if (!safeName(id)) return res.status(400).json({ error: 'invalid id' }); await storageRequest(externalUrl(storagePath(`loader-${id}.txt`)), { method: 'DELETE' }).catch((e) => { if (e.status !== 404) throw e; }); const list = normalizeArray(await readJson('loader-index.json', []), []); await writeJson('loader-index.json', list.filter((v) => v.id !== id)); res.json({ status: 'deletado' }); } catch (e) { jsonError(res, e); } });
app.get('/api/load/:id', async (req, res) => { try { res.type('text/plain').send(await readText(`loader-${req.params.id}.txt`)); } catch (e) { jsonError(res, e); } });

app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'index.html')));
app.use(express.static(__dirname));

export function startServer(port = PORT) {
  return app.listen(port, () => console.log(`Painel Node ativo em http://localhost:${port}`));
}

const isDirectRun = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isDirectRun) startServer(PORT);
