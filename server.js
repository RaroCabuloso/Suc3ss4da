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
const PORT = Number(process.env.PORT || 3000);
const STORAGE_URL = (config.APIFILE_URL || 'https://apifile.netlify.app').replace(/\/$/, '');
const STORAGE_TOKEN = config.APIFILE_ADMIN_TOKEN;
const ADMIN_USER = config.ADMIN_USER || '1v99ByRaro';
const ADMIN_PASS = config.ADMIN_PASS || '199';
const ROOT = '/suc3ss4da';
const sessions = new Set();
const cache = new Map();

export default app;

app.use(cors());
app.use(express.json({ limit: '5mb' }));

function auth(req, res, next) {
  const token = req.get('Authorization');
  if (!token || !sessions.has(token)) return res.status(403).json({ error: 'admin apenas' });
  next();
}

function safeName(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.-]+$/.test(value);
}

function storagePath(name) {
  if (!safeName(name)) throw new Error('nome de arquivo inválido');
  return `${ROOT}/${name}`;
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
  const response = await fetch(url, {
    ...options,
    headers: { Authorization: `Bearer ${STORAGE_TOKEN}`, ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) }
  });
  const text = await response.text();
  let body = text;
  try { body = JSON.parse(text); } catch { /* conteúdo textual */ }
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

async function readJson(name, fallback) {
  if (cache.has(name)) return cache.get(name);
  try {
    const body = await storageRequest(externalUrl(storagePath(name)));
    const raw = typeof body === 'string' ? body : body?.data ?? body?.content ?? body;
    const value = typeof raw === 'string' ? JSON.parse(raw) : raw;
    cache.set(name, value);
    return value;
  } catch (error) {
    if (error.status === 404) return fallback;
    throw error;
  }
}

async function writeJson(name, value) {
  await ensureStorageRoot();
  const filePath = storagePath(name);
  const content = JSON.stringify(value, null, 2);
  try {
    await storageRequest(externalUrl(filePath), { method: 'PUT', body: JSON.stringify({ content }) });
  } catch (error) {
    if (error.status !== 404) throw error;
    await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(filePath), content }) });
  }
  cache.set(name, value);
  return value;
}

async function readText(name) {
  const body = await storageRequest(externalUrl(storagePath(name)));
  return typeof body === 'string' ? body : body?.data ?? body?.content ?? '';
}

async function writeText(name, content) {
  await ensureStorageRoot();
  const filePath = storagePath(name);
  try {
    await storageRequest(externalUrl(filePath), { method: 'PUT', body: JSON.stringify({ content }) });
  } catch (error) {
    if (error.status !== 404) throw error;
    await storageRequest(`${STORAGE_URL}/api/files/`, { method: 'POST', body: JSON.stringify({ path: bodyStoragePath(filePath), content }) });
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
  const session = token(); sessions.add(session);
  res.json({ token: session, user: 'admin', admin: true });
});
app.get('/api/admin/verify', auth, (_req, res) => res.json({ admin: true, valid: true }));
app.post('/api/logout', (req, res) => { sessions.delete(req.get('Authorization')); res.json({ success: true }); });

app.get('/api/logs', auth, async (_req, res) => { try { const logs = await readJson('logs.json', []); res.json({ logs, pagination: { page: 1, limit: logs.length, total: logs.length, pages: 1 } }); } catch (e) { jsonError(res, e); } });
app.get('/api/admin/banlist', auth, async (_req, res) => { try { res.json(await readJson('bans.json', [])); } catch (e) { jsonError(res, e); } });
app.get('/api/produtos', auth, async (_req, res) => { try { res.json(await readJson('produtos.json', [])); } catch (e) { jsonError(res, e); } });
app.get('/api/keys', auth, async (_req, res) => { try { res.json(await readJson('keys.json', {})); } catch (e) { jsonError(res, e); } });
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

app.get('/api/stats', auth, async (_req, res) => {
  try {
    const [logs, produtos, keys, scripts, loader] = await Promise.all([readJson('logs.json', []), readJson('produtos.json', []), readJson('keys.json', {}), readJson('scripts.json', []), readJson('loader-index.json', [])]);
    res.json({ players: new Set(logs.map((item) => item.id).filter(Boolean)).size, execucoes: logs.length, scripts: loader.length, raw_scripts: scripts.length, produtos: produtos.length, keys: Object.keys(keys).length });
  } catch (e) { jsonError(res, e); }
});

app.post('/api/admin/ban', auth, async (req, res) => { try { const bans = await readJson('bans.json', []); const entry = { ...req.body, data: new Date().toLocaleDateString('pt-BR'), hora: new Date().toLocaleTimeString('pt-BR') }; if (!bans.some((b) => ['hwid', 'ip', 'nick'].some((key) => entry[key] && b[key] === entry[key]))) await writeJson('bans.json', [...bans, entry]); res.json({ status: 'banido', entry }); } catch (e) { jsonError(res, e); } });
app.post('/api/admin/unban', auth, async (req, res) => { try { const { hwid, ip, nick } = req.body || {}; const bans = await readJson('bans.json', []); await writeJson('bans.json', bans.filter((b) => !((hwid && b.hwid === hwid) || (ip && b.ip === ip) || (nick && b.nick === nick)))); res.json({ status: 'desbanido' }); } catch (e) { jsonError(res, e); } });

function collectionRoutes(collection, idKey = 'id') {
  app.post(`/api/${collection}`, auth, async (req, res) => { try { const list = await readJson(`${collection}.json`, []); const item = { ...req.body, [idKey]: req.body[idKey] || crypto.randomUUID().replaceAll('-', ''), criado: now(), atualizado: now() }; await writeJson(`${collection}.json`, [...list, item]); res.json({ status: 'criado', [collection === 'scripts' ? 'script' : 'produto']: item }); } catch (e) { jsonError(res, e); } });
  app.get(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const item = (await readJson(`${collection}.json`, [])).find((value) => String(value[idKey]) === req.params.id); item ? res.json(item) : res.status(404).json({ error: 'não encontrado' }); } catch (e) { jsonError(res, e); } });
  app.put(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const list = await readJson(`${collection}.json`, []); const index = list.findIndex((value) => String(value[idKey]) === req.params.id); if (index < 0) return res.status(404).json({ error: 'não encontrado' }); list[index] = { ...list[index], ...req.body, atualizado: now() }; await writeJson(`${collection}.json`, list); res.json({ status: 'atualizado', [collection === 'scripts' ? 'script' : 'produto']: list[index] }); } catch (e) { jsonError(res, e); } });
  app.delete(`/api/${collection}/:id`, auth, requireName, async (req, res) => { try { const list = await readJson(`${collection}.json`, []); const next = list.filter((value) => String(value[idKey]) !== req.params.id); if (next.length === list.length) return res.status(404).json({ error: 'não encontrado' }); await writeJson(`${collection}.json`, next); res.json({ status: 'deletado' }); } catch (e) { jsonError(res, e); } });
}
collectionRoutes('produtos');
collectionRoutes('scripts');
app.get('/api/scripts', auth, async (_req, res) => { try { res.json((await readJson('scripts.json', [])).map(({ id, titulo, criado, atualizado }) => ({ id, titulo, criado, atualizado }))); } catch (e) { jsonError(res, e); } });

app.get('/api/raw/:id', async (req, res) => { if (!req.get('User-Agent')?.toLowerCase().includes('roblox') && !req.get('X-Roblox-UserId')) return res.status(403).send('403 Forbidden'); try { const script = (await readJson('scripts.json', [])).find((item) => item.id === req.params.id); if (!script) return res.status(404).send('-- not found'); res.type('text/plain').send(`local Maker = "34hz"\nprint("by 34hz")\n\n${script.codigo || ''}`); } catch (e) { jsonError(res, e); } });

app.get('/api/loader/list', auth, async (_req, res) => { try { res.json(await readJson('loader-index.json', [])); } catch (e) { jsonError(res, e); } });
app.post('/api/loader/save', auth, async (req, res) => { try { const { id, content } = req.body || {}; if (!safeName(id) || content === undefined) return res.status(400).json({ error: 'missing id or content' }); await writeText(`loader-${id}.txt`, content); const list = await readJson('loader-index.json', []); const item = { id, size: Buffer.byteLength(content), modified: new Date().toLocaleString('pt-BR') }; await writeJson('loader-index.json', [...list.filter((v) => v.id !== id), item]); res.json({ status: 'ok', path: id }); } catch (e) { jsonError(res, e); } });
app.post('/api/loader/delete', auth, async (req, res) => { try { const { id } = req.body || {}; if (!safeName(id)) return res.status(400).json({ error: 'invalid id' }); await storageRequest(externalUrl(storagePath(`loader-${id}.txt`)), { method: 'DELETE' }).catch((e) => { if (e.status !== 404) throw e; }); await writeJson('loader-index.json', (await readJson('loader-index.json', [])).filter((v) => v.id !== id)); res.json({ status: 'deletado' }); } catch (e) { jsonError(res, e); } });
app.get('/api/load/:id', async (req, res) => { try { res.type('text/plain').send(await readText(`loader-${req.params.id}.txt`)); } catch (e) { jsonError(res, e); } });

app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'index.html')));
app.use(express.static(__dirname));

export function startServer(port = PORT) {
  return app.listen(port, () => console.log(`Painel Node ativo em http://localhost:${port}`));
}

const isDirectRun = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isDirectRun) startServer(PORT);