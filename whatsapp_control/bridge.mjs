import { createDecipheriv, createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import { chmod, constants, createWriteStream, lstat, mkdir, readdirSync, realpath, rmSync, unlink } from 'node:fs';
import { open } from 'node:fs/promises';
import { promisify } from 'node:util';
import path from 'node:path';
import readline from 'node:readline';
import { DatabaseSync } from 'node:sqlite';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import makeWASocket, {
  Browsers,
  BufferJSON,
  DisconnectReason,
  getBinaryNodeChild,
  getMediaKeys,
  initAuthCreds,
  jidNormalizedUser,
  proto,
} from 'baileys';
import { extractGroupMetadata } from 'baileys/lib/Socket/groups.js';
import pino from 'pino';
import qrcode from 'qrcode-terminal';

const runtimePath = process.argv[2];
if (!runtimePath || !path.isAbsolute(runtimePath)) {
  throw new Error('Expected an absolute private runtime directory');
}
process.umask(0o077);
const dataDir = path.normalize(runtimePath);
const createDirectory = promisify(mkdir);
const fileInfo = promisify(lstat);
const setMode = promisify(chmod);
const removeFile = promisify(unlink);
const mediaDir = path.join(dataDir, 'media');
const scratchDir = path.join(dataDir, 'scratch');
const downloadedFilename = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.(jpg|png|webp|ogg|bin)$/;
const speechDirectory = /^speech-[a-z0-9_]{8}$/;
await createDirectory(dataDir, { recursive: true, mode: 0o700 });
await createDirectory(mediaDir, { mode: 0o700 }).catch((error) => {
  if (error.code !== 'EEXIST') throw error;
});
for (const directory of [dataDir, mediaDir]) {
  if (!(await fileInfo(directory)).isDirectory()) throw new Error('Expected a private runtime directory');
  await setMode(directory, 0o700);
}
const databasePath = path.join(dataDir, 'transport.sqlite3');
const existingDatabase = await fileInfo(databasePath).catch((error) => {
  if (error.code !== 'ENOENT') throw error;
});
if (existingDatabase && !existingDatabase.isFile()) throw new Error('Invalid transport database');
const db = new DatabaseSync(databasePath);
await setMode(databasePath, 0o600);
db.exec(`
  PRAGMA journal_mode = WAL;
  PRAGMA busy_timeout = 3000;
  PRAGMA secure_delete = ON;
  CREATE TABLE IF NOT EXISTS auth (
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account, kind, id)
  );
  CREATE TABLE IF NOT EXISTS contacts (
    account TEXT NOT NULL,
    id TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (account, id)
  );
  CREATE TABLE IF NOT EXISTS contact_aliases (
    account TEXT NOT NULL,
    lid TEXT NOT NULL,
    phone TEXT NOT NULL,
    PRIMARY KEY (account, lid, phone)
  );
  CREATE TABLE IF NOT EXISTS runtime_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1), root_hash TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS outgoing (
    account TEXT NOT NULL,
    id TEXT NOT NULL,
    status TEXT NOT NULL,
    rejection_status INTEGER,
    PRIMARY KEY (account, id)
  );
  CREATE TABLE IF NOT EXISTS group_creations (
    account TEXT NOT NULL,
    request_id TEXT NOT NULL,
    stanza_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (account, request_id),
    UNIQUE (account, stanza_id)
  );
`);
const rootHash = createHash('sha256').update(await promisify(realpath)(dataDir)).digest('hex');
const currentRoot = db.prepare('SELECT root_hash FROM runtime_identity WHERE id = 1').get()?.root_hash;
const hasCredentials = Boolean(db.prepare("SELECT 1 FROM auth WHERE kind = 'creds' LIMIT 1").get());
const copiedProfile = Boolean(hasCredentials && currentRoot && currentRoot !== rootHash);
if (!copiedProfile) {
  db.prepare('INSERT OR REPLACE INTO runtime_identity VALUES (1, ?)').run(rootHash);
  cleanScratch();
  await createDirectory(scratchDir, { mode: 0o700 });
  process.env.TMPDIR = scratchDir;
}
const getKey = db.prepare('SELECT value FROM auth WHERE account = ? AND kind = ? AND id = ?');
const putKey = db.prepare(`
  INSERT INTO auth VALUES (?, ?, ?, ?)
  ON CONFLICT (account, kind, id) DO UPDATE SET value = excluded.value
`);
const dropKey = db.prepare('DELETE FROM auth WHERE account = ? AND kind = ? AND id = ?');
const dropAuth = db.prepare('DELETE FROM auth WHERE account = ?');
const getContact = db.prepare('SELECT value FROM contacts WHERE account = ? AND id = ?');
const listContacts = db.prepare('SELECT value FROM contacts WHERE account = ? ORDER BY id');
const putContact = db.prepare(`
  INSERT INTO contacts VALUES (?, ?, ?)
  ON CONFLICT (account, id) DO UPDATE SET value = excluded.value
`);
const dropContacts = db.prepare('DELETE FROM contacts WHERE account = ?');
const saveContactAlias = db.prepare('INSERT OR IGNORE INTO contact_aliases VALUES (?, ?, ?)');
const listContactAliases = db.prepare(`
  SELECT lid, phone FROM (
    SELECT lid, phone, count(*) OVER (PARTITION BY lid) lids, count(*) OVER (PARTITION BY phone) phones
    FROM contact_aliases WHERE account = ?
  ) WHERE lids = 1 AND phones = 1
`);
const dropContactAliases = db.prepare('DELETE FROM contact_aliases WHERE account = ?');
const getOutgoing = db.prepare('SELECT status, rejection_status FROM outgoing WHERE account = ? AND id = ?');
const startOutgoing = db.prepare("INSERT OR REPLACE INTO outgoing VALUES (?, ?, 'unknown', NULL)");
const updateOutgoing = db.prepare('UPDATE outgoing SET status = ?, rejection_status = ? WHERE account = ? AND id = ?');
const dropOutgoing = db.prepare('DELETE FROM outgoing WHERE account = ?');
const readCreation = db.prepare('SELECT status,result FROM group_creations WHERE account = ? AND request_id = ?');
const awaitingCreation = db.prepare("SELECT 1 FROM group_creations WHERE account = ? AND stanza_id = ? AND status = 'unknown'");
const startCreation = db.prepare("INSERT INTO group_creations VALUES (?, ?, ?, 'unknown', NULL)");
const finishCreation = db.prepare("UPDATE group_creations SET status = ?, result = ? WHERE account = ? AND stanza_id = ? AND status = 'unknown'");
const dropCreations = db.prepare('DELETE FROM group_creations WHERE account = ?');
const sockets = new Map();
const logger = pino({ level: 'silent' });
const maxMediaBytes = 15 * 1024 * 1024;
const maxRecentMessages = 200;
const messageMaxAgeMs = 10 * 60 * 1000;
const historyTypes = proto.HistorySync.HistorySyncType;
const contactHistory = new Set([
  historyTypes.INITIAL_BOOTSTRAP, historyTypes.RECENT, historyTypes.NON_BLOCKING_DATA,
]);
let shuttingDown = false;

function cleanScratch() {
  for (const filename of readdirSync(mediaDir)) {
    if (downloadedFilename.test(filename) || speechDirectory.test(filename)) {
      rmSync(path.join(mediaDir, filename), { recursive: speechDirectory.test(filename), force: true });
    }
  }
  rmSync(scratchDir, { recursive: true, force: true });
}

function transaction(write) {
  db.exec('BEGIN IMMEDIATE');
  try {
    write();
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

function decode(row) {
  return row ? JSON.parse(row.value, BufferJSON.reviver) : undefined;
}

function readKey(entry, type, id) {
  const row = entry.stagedAuth
    ? entry.stagedAuth.get(JSON.stringify([type, id]))
    : getKey.get(entry.account, type, id);
  return decode(row);
}

function saveKey(entry, type, id, value) {
  const encoded = value == null ? null : JSON.stringify(value, BufferJSON.replacer);
  if (entry.stagedAuth) {
    const key = JSON.stringify([type, id]);
    if (encoded == null) entry.stagedAuth.delete(key);
    else entry.stagedAuth.set(key, { value: encoded });
  } else if (encoded == null) dropKey.run(entry.account, type, id);
  else putKey.run(entry.account, type, id, encoded);
}

function authState(entry, isCurrent) {
  const creds = readKey(entry, 'creds', 'current') ?? initAuthCreds();
  return {
    creds,
    keys: {
      async get(type, ids) {
        const result = Object.create(null);
        if (!isCurrent()) return result;
        for (const id of ids) {
          let value = readKey(entry, type, id);
          if (value && type === 'app-state-sync-key') {
            value = proto.Message.AppStateSyncKeyData.fromObject(value);
          }
          result[id] = value;
        }
        return result;
      },
      async set(data) {
        if (!isCurrent()) return;
        transaction(() => {
          for (const [type, values] of Object.entries(data)) {
            for (const [id, value] of Object.entries(values)) {
              saveKey(entry, type, id, value);
            }
          }
        });
      },
    },
  };
}

function normalized(value, fallback = '') {
  if (typeof value !== 'string' || !value) return fallback;
  return jidNormalizedUser(value) || fallback;
}

const phoneJid = /^\d{1,24}(?:-\d{1,24})?@s\.whatsapp\.net$/;
const lidJid = /^\d{1,24}(?:-\d{1,24})?@lid$/;

function jid(value, domain) {
  const id = typeof value === 'string' && /^\d{5,20}$/.test(value)
    ? `${value}@s.whatsapp.net`
    : value;
  if (typeof id !== 'string' || !/^\d{1,24}(?:-\d{1,24})?@(s\.whatsapp\.net|lid|g\.us)$/.test(id)) {
    throw new Error('Expected a WhatsApp number or JID');
  }
  if (domain && (id.endsWith('@g.us') ? 'group' : 'user') !== domain) {
    throw new Error(`Expected a WhatsApp ${domain} address`);
  }
  return id;
}

function jids(values, domain = 'user') {
  if (!Array.isArray(values) || !values.length) throw new Error('Expected at least one destination');
  return [...new Set(values.map((value) => jid(value, domain)))];
}

function identity(user) {
  if (!user?.id) return null;
  const id = normalized(user.id);
  return {
    id,
    lid: normalized(user.lid, id.endsWith('@lid') ? id : ''),
    phone: normalized(user.phoneNumber, id.endsWith('@s.whatsapp.net') ? id : ''),
    name: typeof user.name === 'string' ? user.name : '',
  };
}

function identityIds(...values) {
  return new Set(values.flatMap((value) => [value?.id, value?.lid, value?.phone]).filter(Boolean));
}

function output(value) {
  if (!shuttingDown) process.stdout.write(`${JSON.stringify(value)}\n`);
}

function accountStatus(entry) {
  return { status: entry.status, identity: entry.identity };
}

function publishStatus(entry, status, code = null) {
  entry.status = status;
  output({ event: 'connection', account: entry.account, ...accountStatus(entry), code });
}

function connectionError(message, code) {
  return Object.assign(new Error(message), { code });
}

function boundSocket(entry, socket) {
  return sockets.get(entry.account) === entry && entry.socket === socket;
}

function settle(entry, error) {
  clearTimeout(entry.deadline);
  const pending = entry.pending;
  entry.pending = null;
  entry.ready = null;
  if (!pending) return;
  if (error) pending.reject(error);
  else pending.resolve(accountStatus(entry));
}

function closeEntry(entry, error) {
  clearTimeout(entry.reconnect);
  const socket = entry.socket;
  entry.socket = null;
  entry.authGeneration = null;
  sockets.delete(entry.account);
  settle(entry, error);
  if (socket) void socket.end(undefined).catch(() => {});
}

function rejectPairing(entry, socket, error) {
  entry.loggingOut = true;
  // Only the newly scanned device is revoked. The previous login remains intact.
  void socket.logout().catch(() => {}).finally(() => {
    if (sockets.get(entry.account) !== entry) return;
    publishStatus(entry, 'disconnected', error.code);
    closeEntry(entry, error);
  });
}

function updateContacts(account, changes, fromContactSync = false, fromAliasSync = false) {
  if (!changes?.length) return;
  transaction(() => {
    for (const change of changes) {
      const id = normalized(change.id);
      if (!id.endsWith('@lid') && !id.endsWith('@s.whatsapp.net')) continue;
      const old = decode(getContact.get(account, id));
      const named = typeof change.name === 'string' && Boolean(change.name.trim());
      // A legacy confirmed flag could come from an empty upsert followed by chat history.
      const saved = old?.saved === true || (fromContactSync && named);
      const contact = {
        id,
        lid: normalized(change.lid, old?.lid ?? (id.endsWith('@lid') ? id : '')),
        phone: normalized(change.phoneNumber, old?.phone ?? (id.endsWith('@s.whatsapp.net') ? id : '')),
        name: named && (fromContactSync || !old?.saved) ? change.name : (old?.name ?? ''),
        saved,
      };
      putContact.run(account, id, JSON.stringify(contact));
      if ((fromContactSync && named) || fromAliasSync) {
        const lid = lidJid.test(id) ? id : normalized(change.lid);
        const phone = phoneJid.test(id) ? id : normalized(change.phoneNumber);
        if (lidJid.test(lid) && phoneJid.test(phone)) saveContactAlias.run(account, lid, phone);
      }
    }
  });
}

function chatContent(message) {
  let content = message;
  // Normal disappearing messages still have live chat content. Other wrappers
  // include quoted, edited, view-once, and Status data, which are separate events.
  for (let level = 0; level < 5; level++) {
    const nested = content?.ephemeralMessage?.message ?? content?.documentWithCaptionMessage?.message;
    if (!nested) break;
    content = nested;
  }
  return content;
}

function supportedContent(content) {
  if (!content || content.viewOnceMessage || content.viewOnceMessageV2 || content.viewOnceMessageV2Extension
      || content.imageMessage?.viewOnce || content.audioMessage?.viewOnce || content.videoMessage?.viewOnce) return null;
  if (typeof content.conversation === 'string') return { type: 'text', message: { conversation: content.conversation } };
  for (const [field, type] of Object.entries({
    extendedTextMessage: 'text', imageMessage: 'image', videoMessage: 'video', audioMessage: 'audio', documentMessage: 'document',
  })) {
    if (content[field]) return { type, message: { [field]: content[field] } };
  }
  return null;
}

function messageKey(chat, id) {
  return JSON.stringify([chat, id]);
}

function storedMessage(entry, chat, id) {
  const key = messageKey(chat, id);
  const cached = entry.recentMessages.get(key);
  if (!cached || Date.now() - cached.storedAt <= messageMaxAgeMs) return cached;
  entry.recentMessages.delete(key);
}

function contactAlias(account, sender) {
  if (!sender || sender.endsWith('@g.us')) return '';
  const contact = decode(getContact.get(account, sender));
  return sender.endsWith('@lid') ? contact?.phone || '' : contact?.lid || '';
}

function rememberMessage(entry, chat, id, message) {
  const key = messageKey(chat, id);
  entry.recentMessages.delete(key);
  entry.recentMessages.set(key, { ...message, storedAt: Date.now() });
  if (entry.recentMessages.size > maxRecentMessages) entry.recentMessages.delete(entry.recentMessages.keys().next().value);
}

function handleMessages(entry, { messages, type, requestId }, cacheOwn = false) {
  for (const message of messages) {
    const key = message.key;
    const chat = normalized(key?.remoteJid);
    const isGroup = chat.endsWith('@g.us');
    if (!key?.id || key.isViewOnce || (!isGroup && !chat.endsWith('@lid') && !chat.endsWith('@s.whatsapp.net'))) continue;
    const content = chatContent(message.message);
    const supported = supportedContent(content);
    if (!supported) continue;
    const image = content.imageMessage;
    const audio = content.audioMessage;
    let media = image ? { type: 'image', mime: image.mimetype || 'image/jpeg', view_once: false }
      : audio?.ptt ? { type: 'voice', mime: audio.mimetype || 'audio/ogg', view_once: false }
        : null;
    const text = content.conversation ?? content.extendedTextMessage?.text ?? image?.caption ?? '';
    const sender = normalized(isGroup ? key.participant : key.remoteJid);
    const senderAlt = normalized(isGroup ? key.participantAlt : key.remoteJidAlt) || contactAlias(entry.account, sender);
    const context = content.extendedTextMessage?.contextInfo ?? image?.contextInfo ?? audio?.contextInfo;
    const live = type === 'notify' && !requestId;
    const timestamp = Number(message.messageTimestamp ?? 0);
    let attachment = image ?? audio;
    if (!media && context?.quotedMessage?.imageMessage && !context.quotedMessage.imageMessage.viewOnce) {
      // A quoted preview is sender-supplied data. Use only a previously received original.
      const reference = context.stanzaId && context.stanzaId !== key.id
        && (!context.remoteJid || normalized(context.remoteJid) === chat)
        ? storedMessage(entry, chat, context.stanzaId)
        : undefined;
      attachment = reference?.message?.imageMessage;
      media = { type: 'image', quoted: true, unavailable: !attachment };
      if (attachment) media.mime = attachment.mimetype || 'image/jpeg';
    }
    if (live || cacheOwn) {
      rememberMessage(entry, chat, key.id, {
        ...supported, chat, id: key.id, timestamp, from_me: Boolean(key.fromMe),
        text: String(text || content.videoMessage?.caption || content.documentMessage?.caption || '').slice(0, 500),
        media, attachment,
      });
    }
    if (!text && !media) continue;
    output({
      event: 'message', account: entry.account, chat, id: key.id,
      sender, sender_alt: senderAlt, text, mentions: (context?.mentionedJid ?? []).map((id) => normalized(id)),
      is_group: isGroup, chat_type: isGroup ? 'group' : 'dm', from_me: Boolean(key.fromMe), live, media,
      self_ids: [entry.identity?.id, entry.identity?.lid, entry.identity?.phone].filter(Boolean),
      timestamp,
    });
  }
}

function openSocket(entry) {
  const generation = Symbol();
  entry.authGeneration = generation;
  const auth = authState(entry, () => {
    return sockets.get(entry.account) === entry && entry.authGeneration === generation;
  });
  const socket = makeWASocket({
    auth,
    browser: Browsers.macOS('Chrome'),
    logger,
    syncFullHistory: false,
    shouldSyncHistoryMessage: ({ syncType }) => contactHistory.has(syncType),
    markOnlineOnConnect: false,
    getMessage: async () => undefined,
  });
  entry.socket = socket;
  socket.ws.on('CB:iq', (frame) => {
    if (boundSocket(entry, socket) && !entry.stagedAuth) recordCreationReceipt(entry.account, frame);
  });
  socket.ws.on('CB:ack,class:message', ({ attrs }) => {
    if (!boundSocket(entry, socket) || entry.stagedAuth || !attrs?.id) return;
    const reply = getOutgoing.get(entry.account, attrs.id);
    if (reply?.status !== 'unknown') return;
    const rejected = attrs.error !== undefined;
    updateOutgoing.run(rejected ? 'rejected' : 'accepted', rejected ? Number(attrs.error) || null : null, entry.account, attrs.id);
  });
  socket.ev.on('creds.update', () => {
    if (!boundSocket(entry, socket)) return;
    saveKey(entry, 'creds', 'current', auth.creds);
  });
  socket.ev.on('lid-mapping.update', ({ lid, pn }) => {
    if (!boundSocket(entry, socket) || entry.stagedAuth) return;
    updateContacts(entry.account, [{ id: lid, phoneNumber: pn }, { id: pn, lid }], false, true);
  });
  socket.ev.on('contacts.upsert', (contacts) => {
    if (boundSocket(entry, socket) && !entry.stagedAuth) updateContacts(entry.account, contacts, true);
  });
  socket.ev.on('contacts.update', (contacts) => {
    if (boundSocket(entry, socket) && !entry.stagedAuth) updateContacts(entry.account, contacts);
  });
  socket.ev.on('messaging-history.set', ({ contacts }) => {
    if (boundSocket(entry, socket) && !entry.stagedAuth) updateContacts(entry.account, contacts?.filter((contact) => contact.name));
  });
  socket.ev.on('messages.upsert', (data) => {
    if (boundSocket(entry, socket) && !entry.stagedAuth) handleMessages(entry, data);
  });
  socket.ev.on('connection.update', ({ connection, qr, lastDisconnect }) => {
    if (!boundSocket(entry, socket)) return;
    if (qr && entry.pairing) {
      publishStatus(entry, 'pairing');
      qrcode.generate(qr, { small: true }, (terminal) => output({ event: 'qr', account: entry.account, terminal }));
    }
    if (connection === 'open') {
      const connected = identity(socket.user ?? auth.creds.me);
      if (!connected?.id) {
        publishStatus(entry, 'disconnected', 'identity_unavailable');
        closeEntry(entry, connectionError('WhatsApp did not provide the paired account identity', 'identity_unavailable'));
        return;
      }
      if (entry.stagedAuth) {
        const differentPhone = connected.phone && entry.expectedPhone && connected.phone !== entry.expectedPhone;
        if (differentPhone || (entry.expectedIds && ![...identityIds(connected)].some((id) => entry.expectedIds.has(id)))) {
          rejectPairing(entry, socket, connectionError(
            'Scanned WhatsApp identity does not match this account label. Check Linked Devices on the scanned phone.', 'identity_mismatch',
          ));
          return;
        }
        try {
          saveKey(entry, 'creds', 'current', auth.creds);
          transaction(() => {
            dropAuth.run(entry.account);
            for (const [key, row] of entry.stagedAuth) {
              const [type, id] = JSON.parse(key);
              putKey.run(entry.account, type, id, row.value);
            }
          });
          entry.stagedAuth = null;
        } catch {
          rejectPairing(entry, socket, connectionError('Could not persist the new session', 'connection_failed'));
          return;
        }
      }
      entry.identity = connected;
      entry.pairing = false;
      entry.failures = 0;
      publishStatus(entry, 'connected');
      settle(entry);
    }
    if (connection !== 'close') return;
    entry.socket = null;
    entry.authGeneration = null;
    const code = lastDisconnect?.error?.output?.statusCode;
    if (entry.loggingOut) return;
    if (code === DisconnectReason.loggedOut || code === DisconnectReason.connectionReplaced
        || code === DisconnectReason.badSession || code === DisconnectReason.multideviceMismatch) {
      publishStatus(entry, 'disconnected', code);
      closeEntry(entry, connectionError('WhatsApp session is unavailable. Check Linked Devices and pair again if needed.', 'session_unavailable'));
      return;
    }
    if (!entry.pairing && !auth.creds.me?.id) {
      publishStatus(entry, 'unpaired');
      closeEntry(entry, connectionError('Pair this account before connecting', 'unpaired'));
      return;
    }
    publishStatus(entry, 'reconnecting', code);
    const delay = code === DisconnectReason.restartRequired ? 0 : Math.min(30_000, 1_000 * 2 ** Math.min(entry.failures++, 5));
    entry.reconnect = setTimeout(() => {
      if (sockets.get(entry.account) !== entry) return;
      try {
        openSocket(entry);
      } catch {
        publishStatus(entry, 'disconnected', 'connection_failed');
        closeEntry(entry, connectionError('Could not reconnect to WhatsApp', 'connection_failed'));
      }
    }, delay);
  });
}

async function connect(account, pairing = false, expectedIds = null, expectedPhone = null) {
  const current = sockets.get(account);
  if (current?.status === 'connected') return accountStatus(current);
  if (current) return current.ready ?? accountStatus(current);
  const saved = decode(getKey.get(account, 'creds', 'current'));
  if (!pairing && !saved?.me?.id) throw connectionError('Pair this account before connecting', 'unpaired');
  const entry = {
    account, status: 'connecting', identity: identity(saved?.me), pairing,
    recentMessages: new Map(), socket: null, failures: 0,
    stagedAuth: pairing ? new Map() : null, expectedIds, expectedPhone,
  };
  sockets.set(account, entry);
  const ready = new Promise((resolve, reject) => { entry.pending = { resolve, reject }; });
  entry.ready = ready;
  entry.deadline = setTimeout(() => {
    publishStatus(entry, 'disconnected', 'timeout');
    closeEntry(entry, connectionError('Timed out connecting to WhatsApp', 'timeout'));
  }, pairing ? 120_000 : 35_000);
  try {
    openSocket(entry);
  } catch {
    closeEntry(entry, connectionError('Could not open WhatsApp connection', 'connection_failed'));
  }
  return ready;
}

async function localMedia(args) {
  if (typeof args.path !== 'string' || !path.isAbsolute(args.path)) throw new Error('Expected an absolute attachment path');
  const info = await fileInfo(args.path).catch(() => null);
  if (!info?.isFile()) throw new Error('Expected a regular attachment file');
  const source = { url: args.path };
  const mime = typeof args.mime === 'string' ? args.mime : undefined;
  const caption = typeof args.caption === 'string' ? args.caption : undefined;
  switch (args.type) {
    case 'image': return { image: source, caption, mimetype: mime };
    // Baileys shells out to ffmpeg for video thumbnails using an unquoted path.
    // Supply an explicit (empty) thumbnail and let WhatsApp render the preview.
    case 'video': return { video: source, caption, mimetype: mime, jpegThumbnail: Buffer.alloc(0) };
    case 'voice': return { audio: source, mimetype: 'audio/ogg; codecs=opus', ptt: true };
    case 'audio': return { audio: source, mimetype: mime || 'audio/mpeg', ptt: Boolean(args.ptt) };
    case 'document':
      return { document: source, mimetype: mime || 'application/octet-stream', fileName: path.basename(args.filename || args.path), caption };
    default: throw new Error('Unsupported media type');
  }
}

async function checkStatusVoice(file) {
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK).catch(() => null);
  if (!handle) throw new Error('Expected a regular attachment file');
  try {
    if (!(await handle.stat()).isFile()) throw new Error('Expected a regular attachment file');
    // Ogg Opus starts with a BOS page containing a single identification packet.
    const header = Buffer.alloc(47);
    const { bytesRead } = await handle.read(header, 0, header.length, 0);
    if (bytesRead !== header.length || header.toString('ascii', 0, 4) !== 'OggS'
        || header[4] !== 0 || header[5] !== 2 || header[26] !== 1 || header[27] !== 19
        || header.toString('ascii', 28, 36) !== 'OpusHead' || header[36] !== 1 || header[37] !== 1
        || header[46] !== 0) {
      throw new Error('Voice Status requires a mono Ogg Opus audio file');
    }
  } finally {
    await handle.close();
  }
}

async function sendFile(entry, chat, args, options) {
  const { expected_sha256: expected } = args;
  const voiceStatus = chat === 'status@broadcast' && args.type === 'voice';
  if (expected === undefined) {
    const content = await localMedia(args);
    if (voiceStatus) await checkStatusVoice(args.path);
    return send(entry, chat, content, args.message_id, options);
  }
  if (typeof expected !== 'string' || !/^[a-f0-9]{64}$/.test(expected)) throw new Error('Invalid attachment digest');
  if (typeof args.path !== 'string' || !path.isAbsolute(args.path)) throw new Error('Expected an absolute attachment path');
  const snapshot = path.join(scratchDir, `send-${randomUUID()}`);
  const handle = await open(args.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK).catch(() => null);
  if (!handle) throw new Error('Expected a regular attachment file');
  try {
    if (!(await handle.stat()).isFile()) throw new Error('Expected a regular attachment file');
    const hash = createHash('sha256');
    const measure = new Transform({ transform(chunk, _encoding, done) { hash.update(chunk); done(null, chunk); } });
    await pipeline(handle.createReadStream(), measure, createWriteStream(snapshot, { flags: 'wx', mode: 0o600 }));
    if (hash.digest('hex') !== expected) throw new Error('The attachment changed after the owner selected it');
    if (voiceStatus) await checkStatusVoice(snapshot);
    const content = await localMedia({ ...args, path: snapshot, filename: path.basename(args.path) });
    return await send(entry, chat, content, args.message_id, options);
  } finally {
    try {
      await handle.close();
    } finally {
      await removeFile(snapshot).catch(() => {});
    }
  }
}

function textArgument(value, label) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`Expected ${label}`);
  return value;
}

function requireSocket(account) {
  const entry = sockets.get(account);
  if (!entry?.socket || entry.status !== 'connected' || entry.loggingOut || shuttingDown) {
    throw connectionError('WhatsApp account is not connected', 'offline');
  }
  return entry;
}

function groupSummary(group) {
  return {
    id: group.id,
    subject: group.subject,
    description: group.desc ?? '',
    announce: Boolean(group.announce),
    restrict: Boolean(group.restrict),
    participants: (group.participants ?? []).map(({ id, lid, phoneNumber, admin }) => ({
      id, lid, phone: phoneNumber, admin: admin ?? null,
    })),
  };
}

function recordCreationReceipt(account, frame) {
  const id = frame?.attrs?.id;
  if (!id || frame.tag !== 'iq' || !['result', 'error'].includes(frame.attrs.type)) return;
  if (!awaitingCreation.get(account, id)) return;
  const error = getBinaryNodeChild(frame, 'error');
  const status = Number(error?.attrs?.code);
  if (error || frame.attrs.type === 'error') {
    if (definitiveGroupRefusals.has(status)) {
      finishCreation.run('rejected', JSON.stringify({ rejection_status: status }), account, id);
    }
    return;
  }
  if (!getBinaryNodeChild(frame, 'group')) return;
  let group;
  try {
    group = groupSummary(extractGroupMetadata(frame));
    jid(group.id, 'group');
  } catch {
    return;
  }
  finishCreation.run('accepted', JSON.stringify({ group }), account, id);
}

function groupCreationResult(account, requestId) {
  const receipt = readCreation.get(account, requestId);
  return { request_id: requestId, status: receipt?.status ?? 'unknown', ...JSON.parse(receipt?.result ?? '{}') };
}

async function createGroup(entry, args) {
  const requestId = wireId(args.request_id);
  const subject = textArgument(args.subject, 'a group subject');
  const participants = Array.isArray(args.participants) && !args.participants.length ? [] : jids(args.participants);
  const account = entry.account;
  if (readCreation.get(account, requestId)) return groupCreationResult(account, requestId);

  // Baileys' convenience groupCreate generates its group key and IQ ID internally.
  // Use the same IQ with an explicit stanza ID so an authenticated reply can be
  // stored even after the caller stopped waiting. Never reuse an uncertain ID.
  const socket = entry.socket;
  const id = socket.generateMessageTag();
  startCreation.run(account, requestId, id);
  try {
    const response = await socket.query({
      tag: 'iq', attrs: { id, type: 'set', xmlns: 'w:g2', to: '@g.us' },
      content: [{ tag: 'create', attrs: { subject, key: requestId },
        content: participants.map((jid) => ({ tag: 'participant', attrs: { jid } })) }],
    });
    if (response?.attrs?.id === id) recordCreationReceipt(account, response);
  } catch {
    // A known refusal is recorded from the correlated IQ, even when query()
    // throws it. Connection failures keep the operation unresolved.
  }
  return groupCreationResult(account, requestId);
}

async function download(entry, { chat, id }) {
  const cached = storedMessage(entry, jid(chat), textArgument(id, 'a message ID'));
  if (!cached?.media || !cached.attachment) throw new Error('Incoming media is not available');
  const { media, attachment } = cached;
  // Only fetch from the WhatsApp CDN. Message senders provide attachment metadata.
  let mediaUrl;
  if (typeof attachment.directPath === 'string' && /^\/(?![\/\\])[^\\\s]*$/.test(attachment.directPath)) {
    mediaUrl = new URL(attachment.directPath, 'https://mmg.whatsapp.net');
  } else if (attachment.url) {
    mediaUrl = new URL(attachment.url);
  }
  if (!mediaUrl || mediaUrl.protocol !== 'https:' || !mediaUrl.hostname.endsWith('.whatsapp.net')
      || mediaUrl.username || mediaUrl.password || mediaUrl.port) {
    throw new Error('Invalid WhatsApp attachment location');
  }
  if (Number(attachment.fileLength) > maxMediaBytes) throw new Error('Incoming media is too large');
  const extensions = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'audio/ogg': '.ogg' };
  const filename = path.join(mediaDir, randomUUID() + (extensions[media.mime.split(';')[0]] || '.bin'));
  const socket = entry.socket;
  const signal = AbortSignal.timeout(30_000);
  let size = 0;
  let total = 0;
  const macLength = 10;
  let tail = Buffer.alloc(0);
  const { iv, cipherKey, macKey } = await getMediaKeys(attachment.mediaKey, media.type === 'voice' ? 'audio' : 'image');
  const verifier = createHmac('sha256', macKey).update(iv);
  const cipher = createDecipheriv('aes-256-cbc', cipherKey, iv);
  const decrypt = new Transform({
    transform(chunk, _encoding, done) {
      total += chunk.length;
      if (total > maxMediaBytes + 16 + macLength) {
        done(new Error('Incoming media is too large'));
        return;
      }
      const data = Buffer.concat([tail, chunk]);
      const end = Math.max(0, data.length - macLength);
      tail = data.subarray(end);
      try {
        const encrypted = data.subarray(0, end);
        verifier.update(encrypted);
        const clear = cipher.update(encrypted);
        size += clear.length;
        done(null, clear);
      } catch (error) {
        done(error);
      }
    },
    flush(done) {
      try {
        const expected = verifier.digest().subarray(0, macLength);
        if (tail.length !== macLength || !timingSafeEqual(expected, tail)) {
          throw new Error('Incoming media failed integrity verification');
        }
        const clear = cipher.final();
        size += clear.length;
        if (size > maxMediaBytes) throw new Error('Incoming media is too large');
        done(null, clear);
      } catch (error) {
        done(error);
      }
    },
  });
  try {
    const response = await fetch(mediaUrl, {
      signal, redirect: 'error', headers: { Origin: 'https://web.whatsapp.com' },
    });
    if (!response.ok || !response.body) {
      await response.body?.cancel();
      throw new Error('Incoming media is not available');
    }
    await pipeline(Readable.fromWeb(response.body), decrypt, createWriteStream(filename, { flags: 'wx', mode: 0o600 }), { signal });
    if (!size || !boundSocket(entry, socket)) throw new Error('Incoming media is no longer available');
    return { path: filename, ...media };
  } catch (error) {
    await removeFile(filename).catch(() => {});
    throw error;
  }
}

function wireId(messageId) {
  if (typeof messageId !== 'string' || !/^[A-Za-z0-9_-]{8,80}$/.test(messageId)) throw new Error('Invalid message ID');
  return messageId;
}

function rejection(status) {
  const detail = Number.isInteger(status) ? ` (${status})` : '';
  return Object.assign(connectionError(`WhatsApp explicitly rejected the operation${detail}`, 'rejected'), { status });
}

function sendResult(account, id) {
  const receipt = getOutgoing.get(account, id);
  return { message_id: id, status: receipt?.status ?? 'unknown',
    ...(receipt?.rejection_status == null ? {} : { rejection_status: receipt.rejection_status }) };
}

async function send(entry, chat, content, messageId, options = {}) {
  const id = messageId == null ? `3EB0${randomUUID().replaceAll('-', '').slice(0, 18).toUpperCase()}` : wireId(messageId);
  if (requireSocket(entry.account) !== entry) throw connectionError('WhatsApp connection changed', 'offline');
  const socket = entry.socket;
  const previous = getOutgoing.get(entry.account, id);
  if (previous?.status === 'accepted') return sendResult(entry.account, id);
  if (previous?.status === 'unknown') throw connectionError('The previous send did not return a receipt', 'unknown');
  startOutgoing.run(entry.account, id);
  let notified;
  const ack = new Promise((resolve) => { notified = resolve; });
  const onAck = ({ attrs }) => { if (attrs?.id === id) notified(); };
  socket.ws.on('CB:ack,class:message', onAck);
  let deadline;
  let sent;
  try {
    sent = await socket.sendMessage(chat, content, { ...options, messageId: id });
  } catch (error) {
    // This error comes from Baileys' upload stage, before the message is relayed.
    if (error.output?.statusCode === 500 && error.message === 'Media upload failed on all hosts'
        && sendResult(entry.account, id).status === 'unknown') {
      updateOutgoing.run('rejected', null, entry.account, id);
    }
  }
  try {
    if (sendResult(entry.account, id).status === 'unknown' && boundSocket(entry, socket)) {
      const wait = new Promise((resolve) => { deadline = setTimeout(resolve, 12_000); });
      await Promise.race([ack, wait]);
    }
  } finally {
    clearTimeout(deadline);
    socket.ws.off('CB:ack,class:message', onAck);
  }
  const result = sendResult(entry.account, id);
  if (result.status === 'rejected') throw rejection(result.rejection_status);
  if (result.status === 'unknown') throw connectionError('WhatsApp did not return a matching server receipt', 'unknown');
  if (sent && !storedMessage(entry, chat, id)) handleMessages(entry, { messages: [sent], type: 'append' }, true);
  return result;
}

async function execute({ op, account, args = {} }) {
  if (copiedProfile) throw connectionError('This WhatsApp data was copied from another profile. Use the original profile or purge these copied sessions and pair again.', 'profile_mismatch');
  if (op === 'ping') return { ready: true };
  if (typeof account !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(account)) {
    throw new Error('Invalid account identifier');
  }
  if (op === 'pair' || op === 'connect') return connect(account, op === 'pair');
  if (op === 'repair') {
    const current = sockets.get(account);
    if (current?.status === 'connected') throw new Error('Disconnect the current WhatsApp device before pairing again');
    const saved = identity(decode(getKey.get(account, 'creds', 'current'))?.me);
    const expectedIds = identityIds(saved, args.identity);
    if (!expectedIds.size) throw new Error('Unable to verify the original WhatsApp identity');
    if (current) closeEntry(current, connectionError('WhatsApp connection replaced by pairing', 'offline'));
    return connect(account, true, expectedIds, saved?.phone || args.identity?.phone);
  }
  if (op === 'send_result') return sendResult(account, wireId(args.message_id));
  if (op === 'group_create_result') return groupCreationResult(account, wireId(args.request_id));
  if (op === 'status') {
    const entry = sockets.get(account);
    if (entry) return accountStatus(entry);
    const saved = identity(decode(getKey.get(account, 'creds', 'current'))?.me);
    return { status: saved ? 'disconnected' : 'unpaired', identity: saved };
  }
  if (op === 'disconnect') {
    const entry = sockets.get(account);
    if (entry) closeEntry(entry, connectionError('WhatsApp connection stopped', 'offline'));
    return { status: 'disconnected' };
  }
  if (op === 'remove_account') {
    if (sockets.has(account)) throw new Error('Disconnect this account before removing local data');
    transaction(() => {
      dropAuth.run(account);
      dropContacts.run(account);
      dropContactAliases.run(account);
      dropOutgoing.run(account);
      dropCreations.run(account);
    });
    db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
    return { removed: true };
  }
  if (op === 'discard_media') {
    if (typeof args.path !== 'string' || path.dirname(args.path) !== mediaDir
        || !downloadedFilename.test(path.basename(args.path))) throw new Error('Invalid temporary media path');
    await removeFile(args.path).catch((error) => { if (error.code !== 'ENOENT') throw error; });
    return { removed: true };
  }
  if (op === 'contacts') {
    const contacts = new Map();
    const own = sockets.get(account)?.identity ?? identity(decode(getKey.get(account, 'creds', 'current'))?.me);
    const ownIds = new Set([own?.id, own?.phone, own?.lid].filter(Boolean));
    const savedOnly = args.saved_only === true;
    const aliases = new Map();
    if (savedOnly) {
      for (const { lid, phone } of listContactAliases.all(account)) {
        aliases.set(lid, phone);
        aliases.set(phone, lid);
      }
    }
    for (const row of listContacts.all(account)) {
      const contact = decode(row);
      const saved = contact.saved === true;
      if (savedOnly) {
        const { id, name } = contact;
        if (!saved || !name?.trim() || (!phoneJid.test(id) && !lidJid.test(id))) continue;
        const alias = aliases.get(id);
        if (ownIds.has(id) || ownIds.has(alias)) continue;
        contacts.set(id, { id, name, saved, phone: phoneJid.test(id) ? id : alias || '',
          lid: lidJid.test(id) ? id : alias || '' });
        continue;
      }
      if (!contact.name?.trim() || [contact.id, contact.phone, contact.lid].some((id) => ownIds.has(id))) continue;
      if (!phoneJid.test(contact.id) && !lidJid.test(contact.id)) continue;
      const phone = phoneJid.test(contact.phone) ? contact.phone : '';
      const lid = lidJid.test(contact.lid) ? contact.lid : '';
      const key = phone || contact.id;
      const previous = contacts.get(key);
      const current = { id: contact.id, name: contact.name, phone, lid };
      const preferred = previous?.saved && !saved ? previous : current;
      contacts.set(key, { ...preferred, lid: preferred.lid || previous?.lid || lid,
        saved: saved || Boolean(previous?.saved) });
    }
    return [...contacts.values()];
  }
  if (op === 'logout') {
    const entry = requireSocket(account);
    entry.loggingOut = true;
    try {
      await entry.socket.logout();
      return { local_disconnected: true, unlink_requested: true, remote_unlink_confirmed: false };
    } finally {
      closeEntry(entry, connectionError('WhatsApp connection stopped', 'offline'));
    }
  }
  const entry = requireSocket(account);
  const socket = entry.socket;
  switch (op) {
    case 'send_text':
      return send(entry, jid(args.chat), { text: textArgument(args.text, 'message text') }, args.message_id);
    case 'send_media':
      return sendFile(entry, jid(args.chat), args);
    case 'recent_messages': {
      const chat = jid(args.chat);
      const summary = ({ id, type, timestamp, text, from_me }) => ({ id, type, timestamp, text, from_me });
      if (args.message_id !== undefined) {
        const message = storedMessage(entry, chat, textArgument(args.message_id, 'a message ID'));
        return message ? [summary(message)] : [];
      }
      const limit = args.limit ?? 10;
      if (!Number.isInteger(limit) || limit < 1 || limit > 30) throw new Error('Message list limit must be between 1 and 30');
      return [...entry.recentMessages.values()].reverse()
        .filter((item) => item.chat === chat && Date.now() - item.storedAt <= messageMaxAgeMs)
        .slice(0, limit).map(summary);
    }
    case 'forward_message': {
      const chat = jid(args.source_chat);
      const cached = storedMessage(entry, chat, textArgument(args.source_id, 'a message ID'));
      if (!cached) throw new Error('The source message is not available in the recent messages for this chat');
      return send(entry, jid(args.chat), {
        forward: { key: { id: cached.id, fromMe: cached.from_me, remoteJid: chat }, message: cached.message },
        force: true,
      }, args.message_id);
    }
    case 'post_status': {
      const recipients = jids(args.audience);
      if (!['text', 'image', 'video', 'voice'].includes(args.type)) throw new Error('Unsupported Status media type');
      const options = { broadcast: true, statusJidList: recipients, backgroundColor: '#303030', font: 0 };
      return args.type === 'text'
        ? send(entry, 'status@broadcast', { text: textArgument(args.text, 'Status text') }, args.message_id, options)
        : sendFile(entry, 'status@broadcast', args, options);
    }
    case 'group_list':
      return Object.values(await socket.groupFetchAllParticipating()).map(groupSummary);
    case 'group_metadata':
      return groupSummary(await socket.groupMetadata(jid(args.chat, 'group')));
    case 'group_create':
      return createGroup(entry, args);
    case 'group_members': {
      if (!['add', 'remove', 'promote', 'demote'].includes(args.action)) throw new Error('Unsupported participant action');
      const participants = jids(args.participants);
      const changes = await socket.groupParticipantsUpdate(jid(args.chat, 'group'), participants, args.action);
      return changes.map((change) => {
        const id = normalized(change.jid);
        const original = participants.find((candidate) => candidate === id)
          ?? participants.find((candidate) => contactAlias(account, candidate) === id);
        return { id: original || id, status: change.status };
      });
    }
    case 'group_settings': {
      const chat = jid(args.chat, 'group');
      const subject = args.subject === undefined ? undefined : textArgument(args.subject, 'a group subject');
      if ((args.description !== undefined && typeof args.description !== 'string')
          || (args.announce !== undefined && typeof args.announce !== 'boolean')
          || (args.restrict !== undefined && typeof args.restrict !== 'boolean')) {
        throw new Error('Invalid group setting value');
      }
      const updated = [];
      const apply = async (field, operation) => {
        if (requireSocket(account).socket !== socket) throw connectionError('WhatsApp connection changed', 'offline');
        await operation();
        updated.push(field);
      };
      try {
        if (subject !== undefined) await apply('subject', () => socket.groupUpdateSubject(chat, subject));
        if (typeof args.description === 'string') await apply('description', () => socket.groupUpdateDescription(chat, args.description));
        if (typeof args.announce === 'boolean') await apply('announce', () => socket.groupSettingUpdate(chat, args.announce ? 'announcement' : 'not_announcement'));
        if (typeof args.restrict === 'boolean') await apply('restrict', () => socket.groupSettingUpdate(chat, args.restrict ? 'locked' : 'unlocked'));
      } catch (error) {
        if (!updated.length) throw error;
        return { updated, incomplete: true };
      }
      if (!updated.length) throw new Error('Expected at least one group setting');
      return { updated };
    }
    case 'download_media': return download(entry, args);
    default: throw new Error('Unsupported bridge operation');
  }
}

const exposedErrors = new Set([
  'Expected an absolute attachment path', 'Expected a regular attachment file', 'Invalid account identifier',
  'Invalid message ID', 'Invalid temporary media path', 'Unsupported bridge operation', 'Unsupported media type',
  'Unsupported Status media type', 'Unsupported participant action', 'Incoming media is not available',
  'Incoming media is too large', 'Incoming media is no longer available',
  'Disconnect this account before removing local data', 'Expected a WhatsApp number or JID',
  'Expected a WhatsApp user address', 'Expected a WhatsApp group address', 'Expected at least one destination',
  'Expected at least one group setting', 'Expected a message ID', 'Expected message text', 'Expected Status text',
  'Expected a group subject',
  'Invalid group setting value', 'Invalid WhatsApp attachment location',
  'Incoming media failed integrity verification',
  'Disconnect the current WhatsApp device before pairing again', 'Unable to verify the original WhatsApp identity',
  'Message list limit must be between 1 and 30', 'The source message is not available in the recent messages for this chat',
  'Invalid attachment digest', 'The attachment changed after the owner selected it',
  'Voice Status requires a mono Ogg Opus audio file',
]);
const publicCodes = new Set([
  'offline', 'unpaired', 'timeout', 'connection_failed', 'session_unavailable', 'identity_unavailable',
  'unknown', 'rejected', 'identity_mismatch', 'profile_mismatch',
]);
const groupWrites = new Set(['group_create', 'group_members', 'group_settings']);
const definitiveGroupRefusals = new Set([400, 401, 403, 404, 405, 406, 409, 413, 415, 429]);

const input = readline.createInterface({ input: process.stdin });
input.on('line', async (line) => {
  let request;
  try {
    request = JSON.parse(line);
    if (request?.id == null) return;
    const result = await execute(request);
    output({ id: request.id, ok: true, result });
  } catch (error) {
    if (request?.id == null) return;
    // Baileys' IQ parser places a remote stanza refusal in numeric `data`.
    if (groupWrites.has(request.op) && definitiveGroupRefusals.has(error.data)) error = rejection(error.data);
    const known = publicCodes.has(error.code);
    const validation = exposedErrors.has(error.message);
    output({ id: request.id, ok: false,
      error: known || validation ? error.message : 'WhatsApp did not confirm the operation',
      code: known ? error.code : validation ? 'invalid' : 'unknown',
      ...(Number.isInteger(error.status) ? { status: error.status } : {}),
    });
  }
});

function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const entry of sockets.values()) closeEntry(entry, connectionError('WhatsApp bridge stopped', 'offline'));
  db.close();
  try {
    if (!copiedProfile) cleanScratch();
  } catch {
    // A killed process can leave scratch behind. The next start reclaims it.
  }
  process.exit(0);
}
input.on('close', shutdown);
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
