import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const fakeBaileys = `
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
export * from ${JSON.stringify(import.meta.resolve('baileys'))};
const first = '15550100101@s.whatsapp.net';
const second = '15550100102@s.whatsapp.net';
const saved = '15550100104@s.whatsapp.net';
const historical = '15550100105@s.whatsapp.net';
const nameless = '15550100106@s.whatsapp.net';
const savedLid = '4021@lid';
const savedLidOnly = '4022@lid';
const legacy = '15550100107@s.whatsapp.net';
const group = '12000000000@g.us';
const other = '12000000001@g.us';
const fixtureIdentity = JSON.parse(process.env.WA_FIXTURE_OWN_IDENTITY || 'null');
let sockets = 0;
let stanzas = 0;
export default function makeSocket({auth}) {
  const ev = new EventEmitter();
  const ws = new EventEmitter();
  const index = ++sockets;
  const user = auth.creds.me ?? fixtureIdentity ?? {id: index === 2 ? second : first};
  const image = {url:'https://example.invalid/image', caption:'original photo'};
  const message = (remoteJid,id,content) => ({key:{remoteJid,id}, message:content, messageTimestamp:1});
  function end() { ev.emit('connection.update',{connection:'close'}); }
  setImmediate(() => {
    if (!auth.creds.me) ev.emit('connection.update',{qr:'synthetic-test-token'});
    auth.creds.me = user;
    ev.emit('creds.update',{});
    ev.emit('connection.update',{connection:'open'});
    ev.emit('contacts.upsert', [
      { id: saved, name: 'Saved contact' },
      { id: savedLidOnly, lid: savedLidOnly, name: 'Saved LID-only contact' },
      { id: nameless },
      { id: user.id, name: 'Own account' },
      ...(process.env.WA_FIXTURE_RESYNC === '1' ? [{ id: legacy, name: 'Resynced contact' }] : []),
    ]);
    ev.emit('messaging-history.set', { contacts: [
      { id: historical, name: 'History only' },
      { id: saved, name: 'Historical display name' },
      { id: savedLid, phoneNumber: saved, name: 'Historical alias' },
      { id: '1234567890123456789012345@lid', phoneNumber: saved, name: 'Malformed alias' },
    ] });
    if (process.env.WA_FIXTURE_SAVED_ALIASES === '1') {
      const authoritative = '15550100108@s.whatsapp.net';
      const conflicting = '15550100109@s.whatsapp.net';
      const mapped = '15550100110@s.whatsapp.net';
      ev.emit('contacts.upsert', [
        { id: authoritative, phoneNumber: authoritative, lid: '4024@lid', name: 'Authoritative PN' },
        { id: '4024@lid', name: 'Authoritative LID' },
        { id: conflicting, lid: '4025@lid', name: 'Conflicting PN' },
        { id: '4025@lid', name: 'Conflicting LID' },
        { id: '4026@lid', name: 'Mapped LID' },
        { id: '4027@lid', name: 'Unambiguous event mapping' },
      ]);
      ev.emit('lid-mapping.update', { lid: '4025@lid', pn: mapped });
      ev.emit('lid-mapping.update', { lid: '4026@lid', pn: mapped });
      ev.emit('lid-mapping.update', { lid: '4027@lid', pn: '15550100111@s.whatsapp.net' });
      ev.emit('messaging-history.set', { contacts: [
        { id: savedLidOnly, phoneNumber: historical, name: 'History-supplied phone' },
        { id: '4024@lid', phoneNumber: historical, name: 'Stale phone for confirmed alias' },
      ] });
    }
    ev.emit('messages.upsert',{type:'notify',messages:[
      message(group,'IMAGE_ID',{imageMessage:image}),
      message(group,'QUOTE_IMAGE_ID',{extendedTextMessage:{text:'what is this?', contextInfo:{stanzaId:'IMAGE_ID',quotedMessage:{imageMessage:image}}}}),
      message(other,'SPOOF_IMAGE_ID',{extendedTextMessage:{text:'wrong chat', contextInfo:{remoteJid:group,stanzaId:'IMAGE_ID',quotedMessage:{imageMessage:image}}}}),
      message(group,'PRIVATE_IMAGE_ID',{imageMessage:{...image,viewOnce:true}}),
      message(group,'TEXT_ID',{conversation:'synthetic forward'}),
    ]});
  });
  return {ev,ws,user,end:async()=>end(),logout:async()=>end(),
    generateMessageTag: () => 'synthetic-stanza-' + (++stanzas),
    async query(node) {
      const id = node.attrs.id;
      const create = node.content[0];
      if (node.tag !== 'iq' || node.attrs.xmlns !== 'w:g2' || node.attrs.to !== '@g.us' ||
          node.attrs.type !== 'set' || create.tag !== 'create' || !create.attrs.key) {
        throw Error('Unexpected group creation request');
      }
      const subject = create.attrs.subject;
      const groupNode = { tag:'group', attrs:{ id:'12000000002', subject }, content:create.content };
      if (subject === 'Wrong receipt') {
        const frame = { tag:'iq', attrs:{ id:'not-the-right-stanza', type:'result' }, content:[groupNode] };
        ws.emit('CB:iq', frame);
        return frame;
      }
      const frame = subject === 'Refused group'
        ? { tag:'iq', attrs:{ id, type:'error' }, content:[{ tag:'error', attrs:{ code:'403' } }] }
        : { tag:'iq', attrs:{ id, type:'result' }, content:[groupNode] };
      if (subject === 'Delayed group') {
        setTimeout(() => ws.emit('CB:iq', frame), 60);
        return undefined;
      }
      ws.emit('CB:iq', frame);
      if (subject === 'Refused group') throw Object.assign(new Error('protocol error'), {data:403});
      return frame;
    },
    async sendMessage(chat, content, options) {
      const {messageId:id} = options;
      const attrs = {id,from:chat};
      if (chat === 'status@broadcast' && content.audio) {
        if (!options.broadcast || options.statusJidList.length !== 1 || options.statusJidList[0] !== first) {
          throw Error('Voice Status changed the selected audience');
        }
        if (!content.ptt || content.mimetype !== 'audio/ogg; codecs=opus' || !options.backgroundColor) {
          throw Error('Voice Status is missing PTT metadata');
        }
        const file = await readFile(content.audio.url);
        if (file.toString('ascii', 28, 36) !== 'OpusHead') throw Error('Expected the selected voice file');
      }
      if (content.document) {
        if (content.fileName !== 'notice.txt') throw Error('Document name was not preserved');
        if ((await readFile(content.document.url,'utf8')) !== 'synthetic attachment') throw Error('Wrong snapshot');
      }
      if (content.text === 'reject') attrs.error = '403';
      if (content.text === 'ambiguous') {
        end();
        throw Error('socket stopped without acknowledgement');
      }
      if (content.forward) {
        if (content.forward.message?.conversation !== 'synthetic forward' || !content.force) throw Error('Expected a native forward');
      }
      ws.emit('CB:ack,class:message',{attrs});
      return {key:{id,remoteJid:chat,fromMe:true},message:{conversation:content.text ?? 'synthetic forwarded'}};
    },
    async groupParticipantsUpdate() {
      throw Object.assign(new Error('protocol error'), {data:403});
    },
  };
}
`;

const fakeUrl = `data:text/javascript,${encodeURIComponent(fakeBaileys)}`;
const resolver = `function resolve(s,c,next) { if (s === 'baileys') return next(${JSON.stringify(fakeUrl)},c); return next(s,c); }`;
const loader = `data:text/javascript,${encodeURIComponent(`export ${resolver}`)}`;
const hook = `import * as mod from 'node:module';
if (mod.registerHooks) mod.registerHooks({ resolve: ${resolver} });
else mod.register(${JSON.stringify(loader)});`;
const bridgeScript = fileURLToPath(new URL('../bridge.mjs', import.meta.url));

function bridge(root, env = process.env) {
  const child = spawn(process.execPath, ['--import', `data:text/javascript,${encodeURIComponent(hook)}`, bridgeScript, root],
    { env, stdio: ['pipe', 'pipe', 'inherit'] });
  const closed = new Promise((resolve, reject) => child.on('error', reject).on('close', resolve));
  const pending = new Map();
  const events = [];
  let sequence = 0;
  createInterface({ input: child.stdout }).on('line', (line) => {
    const message = JSON.parse(line);
    if (message.event) events.push(message);
    else pending.get(message.id)?.(message);
  });
  const request = (op, account = 'shop', args = {}) => {
    const id = ++sequence;
    return Promise.race([closed.then((code) => { throw new Error(`bridge exited with ${code}`); }),
      new Promise((resolve) => {
        pending.set(id, (message) => { pending.delete(id); resolve(message); });
        child.stdin.write(JSON.stringify({ id, op, account, args }) + '\n');
      })]);
  };
  const close = async () => { child.stdin.end(); assert.equal(await closed, 0); };
  return { request, events, close };
}

const group = '12000000000@g.us';
const other = '12000000001@g.us';
const target = '15550100103@s.whatsapp.net';
const monoOpus = Buffer.from(
  'T2dnUwACAAAAAAAAAACE1o34AAAAAP4Cp1gBE09wdXNIZWFkAQE4AYC7AAAAAABPZ2dTAAAAAAAAAAAAAITWjfgBAAAABMu8rQE8' +
  'T3B1c1RhZ3MMAAAATGF2ZjYzLjEuMTAyAQAAABwAAABlbmNvZGVyPUxhdmM2My4xLjEwMiBsaWJvcHVzT2dnUwAEeAwAAAAA' +
  'AACE1o34AgAAAGAIrhsEAwMDA/j//vj//vj//g==', 'base64',
);

test('extra Control QR rejects the bot before saving credentials and can be retried', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-pair-bot-test-'));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const live = bridge(temp);
  const args = { reserved_bot_number: '15550100101' };
  try {
    const refused = await live.request('pair', 'personal', args);
    assert.equal(refused.code, 'reserved_bot');
    assert.match(refused.error, /bot pairing QR/);
    assert.equal((await live.request('status', 'personal')).result.status, 'unpaired');
    const database = new DatabaseSync(path.join(temp, 'transport.sqlite3'), { readOnly: true });
    try {
      assert.equal(database.prepare('SELECT count(*) AS records FROM auth WHERE account = ?').get('personal').records, 0);
    } finally {
      database.close();
    }
    assert.equal(live.events.some((event) => event.account === 'personal' && event.status === 'connected'), false);
    const paired = await live.request('pair', 'personal', args);
    assert.equal(paired.result.identity.phone, '15550100102@s.whatsapp.net');
    await live.request('disconnect', 'personal');
    const repair = await live.request('repair', 'personal', { ...args, identity: paired.result.identity });
    assert.equal(repair.code, 'reserved_bot');
    assert.equal((await live.request('status', 'personal')).result.identity.phone, paired.result.identity.phone);
  } finally {
    await live.close();
  }
});

test('extra Control QR requires authenticated phone evidence for LID accounts', { timeout: 20000 }, async (t) => {
  const args = { reserved_bot_number: '15550100101' };
  const cases = [
    [{ id: '15550100101:7@s.whatsapp.net', phoneNumber: '15550100102@s.whatsapp.net' }, 'reserved_bot'],
    [{ id: '4021@lid', phoneNumber: '15550100101:7@s.whatsapp.net' }, 'reserved_bot'],
    [{ id: '4021@lid' }, 'identity_unavailable'],
    [{ id: '15550100102@s.whatsapp.net', phoneNumber: '15550100103@s.whatsapp.net' }, 'identity_unavailable'],
    [{ id: '4021@lid', phoneNumber: '15550100102@s.whatsapp.net' }, null],
  ];
  for (const [user, code] of cases) {
    const temp = await mkdtemp(path.join(tmpdir(), 'wa-pair-lid-test-'));
    t.after(() => rm(temp, { recursive: true, force: true }));
    const live = bridge(temp, { ...process.env, WA_FIXTURE_OWN_IDENTITY: JSON.stringify(user) });
    try {
      const paired = await live.request('pair', 'personal', args);
      if (code) {
        assert.equal(paired.code, code);
        assert.equal((await live.request('status', 'personal')).result.status, 'unpaired');
      } else {
        assert.equal(paired.result.identity.phone, '15550100102@s.whatsapp.net');
      }
    } finally {
      await live.close();
    }
  }
});

test('contact roster distinguishes saved names from history and empty upserts', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-contacts-test-'));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const legacy = '15550100107@s.whatsapp.net';
  const legacyLid = '4023@lid';
  const old = new DatabaseSync(path.join(temp, 'transport.sqlite3'));
  try {
    old.exec('CREATE TABLE contacts (account TEXT, id TEXT, value TEXT, PRIMARY KEY (account, id))');
    old.prepare('INSERT INTO contacts VALUES (?, ?, ?)').run('shop', legacy, JSON.stringify({
      id: legacy, phone: legacy, lid: legacyLid, name: 'Legacy contact name', confirmed: true,
    }));
  } finally {
    old.close();
  }
  const live = bridge(temp);
  try {
    assert.equal((await live.request('pair')).ok, true);
    const { result } = await live.request('contacts');
    assert.deepEqual(result.map(({ phone, name, lid, saved }) => ({ phone, name, lid, saved }))
      .sort((a, b) => (a.phone || a.lid).localeCompare(b.phone || b.lid)), [
      { phone: '15550100104@s.whatsapp.net', name: 'Saved contact', lid: '4021@lid', saved: true },
      { phone: '15550100105@s.whatsapp.net', name: 'History only', lid: '', saved: false },
      { phone: legacy, name: 'Legacy contact name', lid: legacyLid, saved: false },
      { phone: '', name: 'Saved LID-only contact', lid: '4022@lid', saved: true },
    ]);
  } finally {
    await live.close();
  }
  const restarted = bridge(temp, { ...process.env, WA_FIXTURE_RESYNC: '1' });
  try {
    assert.equal((await restarted.request('connect')).ok, true);
    const { result } = await restarted.request('contacts');
    assert.deepEqual(result.find(({ id }) => id === legacy), {
      id: legacy, phone: legacy, lid: legacyLid, name: 'Resynced contact', saved: true,
    });
  } finally {
    await restarted.close();
  }
});

test('saved contact query keeps original addresses and only unambiguous confirmed aliases', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-saved-aliases-test-'));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const live = bridge(temp, { ...process.env, WA_FIXTURE_SAVED_ALIASES: '1' });
  try {
    assert.equal((await live.request('pair')).ok, true);
    const { result } = await live.request('contacts', 'shop', { saved_only: true });
    assert.deepEqual(result.map(({ id, phone, lid, saved }) => [id, phone, lid, saved]), [
      ['15550100104@s.whatsapp.net', '15550100104@s.whatsapp.net', '', true],
      ['15550100108@s.whatsapp.net', '15550100108@s.whatsapp.net', '4024@lid', true],
      ['15550100109@s.whatsapp.net', '15550100109@s.whatsapp.net', '', true],
      ['4022@lid', '', '4022@lid', true],
      ['4024@lid', '15550100108@s.whatsapp.net', '4024@lid', true],
      ['4025@lid', '', '4025@lid', true],
      ['4026@lid', '', '4026@lid', true],
      ['4027@lid', '15550100111@s.whatsapp.net', '4027@lid', true],
    ]);
  } finally {
    await live.close();
  }
});

// The fake socket drives real bridge framing, cache and SQLite; no QR or message leaves the test.
test('re-pair, forward, quoted image, receipts, and copied-profile guard', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-bridge-test-'));
  const original = path.join(temp, 'original');
  const copy = path.join(temp, 'copied');
  t.after(() => rm(temp, { recursive: true, force: true }));
  const media = path.join(original, 'media');
  const stale = path.join(media, 'speech-test0001');
  await mkdir(stale, { recursive: true });
  await writeFile(path.join(stale, 'reply.txt'), 'synthesis fixture');
  const live = bridge(original);
  t.after(live.close);
  assert.equal((await live.request('ping')).ok, true);
  assert.deepEqual(await readdir(media), []);
  assert.equal((await live.request('pair')).result.identity.phone, '15550100101@s.whatsapp.net');
  assert(live.events.some((event) => event.event === 'qr'));
  const messages = (await live.request('recent_messages', 'shop', { chat: group })).result;
  assert.deepEqual(messages.map(({id}) => id), ['TEXT_ID', 'QUOTE_IMAGE_ID', 'IMAGE_ID']);
  assert.equal(live.events.find(({ id }) => id === 'QUOTE_IMAGE_ID')?.media?.unavailable, false);
  assert.equal(live.events.find(({ id }) => id === 'SPOOF_IMAGE_ID')?.media?.unavailable, true);
  const quote = await live.request('download_media', 'shop', { chat: group, id: 'QUOTE_IMAGE_ID' });
  assert.match(quote.error, /attachment location/);
  const wrongQuote = await live.request('download_media', 'shop', { chat: other, id: 'SPOOF_IMAGE_ID' });
  assert.match(wrongQuote.error, /not available/);
  const forward = { chat: target, source_chat: group, source_id: 'TEXT_ID', message_id: 'FORWARD_A1' };
  assert.deepEqual((await live.request('forward_message', 'shop', forward)).result, { message_id: 'FORWARD_A1', status: 'accepted' });
  assert.equal((await live.request('forward_message', 'shop', { ...forward, source_chat: other })).code, 'invalid');
  assert.equal((await live.request('forward_message', 'shop', { ...forward, source_id: 'PRIVATE_IMAGE_ID' })).code, 'invalid');
  const attachment = path.join(temp, 'notice.txt');
  await writeFile(attachment, 'synthetic attachment');
  const digest = createHash('sha256').update('synthetic attachment').digest('hex');
  const mediaSend = { chat: target, type: 'document', path: attachment, message_id: 'DOCUMENT_A1', expected_sha256: digest };
  assert.equal((await live.request('send_media', 'shop', mediaSend)).result.status, 'accepted');
  await writeFile(attachment, 'synthetic changed attachment');
  assert.equal((await live.request('send_media', 'shop', { ...mediaSend, message_id: 'DOCUMENT_A2' })).code, 'invalid');
  assert.deepEqual(await readdir(path.join(original, 'scratch')), []);
  const refused = await live.request('send_text', 'shop', { chat: target, text: 'reject', message_id: 'REJECTED_A1' });
  assert.equal(refused.code, 'rejected');
  assert.equal(refused.status, 403);
  const groupFailure = await live.request('group_members', 'shop', { chat: group, participants:[target], action:'add' });
  assert.equal(groupFailure.code, 'rejected');
  assert.equal(groupFailure.status, 403);
  const uncertain = await live.request('send_text', 'shop', { chat: target, text: 'ambiguous', message_id: 'UNKNOWN_A1' });
  assert.equal(uncertain.code, 'unknown');
  await live.request('disconnect');
  assert.equal((await live.request('repair')).code, 'identity_mismatch');
  assert.equal((await live.request('status')).result.identity.phone, '15550100101@s.whatsapp.net');
  assert.equal((await live.request('repair')).result.identity.phone, '15550100101@s.whatsapp.net');
  await mkdir(stale);
  await writeFile(path.join(stale, 'reply.txt'), 'more synthetic data');
  await live.close();
  await assert.rejects(() => readFile(path.join(stale, 'reply.txt')), { code: 'ENOENT' });
  await cp(original, copy, { recursive: true });
  const wrong = bridge(copy);
  t.after(wrong.close);
  assert.equal((await wrong.request('ping')).code, 'profile_mismatch');
  await wrong.close();
  const restarted = bridge(original);
  t.after(restarted.close);
  assert.equal((await restarted.request('ping')).ok, true);
  assert.equal((await restarted.request('send_result', 'shop', { message_id: 'UNKNOWN_A1' })).result.status, 'unknown');
  assert.deepEqual((await restarted.request('send_result', 'shop', { message_id: 'REJECTED_A1' })).result,
    { message_id: 'REJECTED_A1', status: 'rejected', rejection_status: 403 });
  assert.equal((await restarted.request('send_result', 'shop', { message_id: 'FORWARD_A1' })).result.status, 'accepted');
  await restarted.close();
});

test('group creation reconciles only exact IQ responses and keeps receipts across restart', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-group-receipt-'));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const live = bridge(temp);
  t.after(live.close);
  assert.equal((await live.request('pair')).ok, true);
  const request = (subject, request_id) => live.request('group_create', 'shop', { subject, participants:[target], request_id });
  const lookup = (request_id, account = 'shop') => live.request('group_create_result', account, { request_id });

  assert.equal((await request('Wrong receipt', 'GROUP_WRONG01')).result.status, 'unknown');
  assert.equal((await lookup('GROUP_WRONG01')).result.status, 'unknown');
  assert.deepEqual((await request('Refused group', 'GROUP_REFUSED01')).result,
    { request_id:'GROUP_REFUSED01', status:'rejected', rejection_status:403 });
  assert.equal((await request('Delayed group', 'GROUP_DELAYED01')).result.status, 'unknown');
  // The first request is unresolved and its repeated request ID must not trigger a new IQ.
  assert.equal((await request('Immediate duplicate', 'GROUP_DELAYED01')).result.status, 'unknown');
  const end = Date.now() + 1000;
  let result;
  do {
    result = (await lookup('GROUP_DELAYED01')).result;
    if (result.status !== 'unknown') break;
    await new Promise((resolve) => setTimeout(resolve, 20));
  } while (Date.now() < end);
  assert.equal(result.status, 'accepted');
  assert.equal(result.group.id, '12000000002@g.us');
  assert.equal(result.group.subject, 'Delayed group');
  assert.equal(result.group.participants[0].id, target);
  assert.equal((await lookup('GROUP_DELAYED01', 'other')).result.status, 'unknown');

  await live.close();
  const restarted = bridge(temp);
  t.after(restarted.close);
  assert.deepEqual((await restarted.request('group_create_result', 'shop', { request_id:'GROUP_DELAYED01' })).result, result);
  assert.equal((await restarted.request('remove_account')).ok, true);
  assert.equal((await restarted.request('group_create_result', 'shop', { request_id:'GROUP_DELAYED01' })).result.status, 'unknown');
  await restarted.close();
});

test('Voice Status uses the selected audience and rejects incompatible media before dispatch', { timeout: 20000 }, async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), 'wa-voice-status-test-'));
  const live = bridge(path.join(temp, 'profile'));
  t.after(async () => {
    await live.close();
    await rm(temp, { recursive: true, force: true });
  });
  assert.equal((await live.request('pair')).ok, true);
  const voice = path.join(temp, 'sample.ogg');
  await writeFile(voice, monoOpus);
  const audience = ['15550100101'];
  const base = { type: 'voice', path: voice, audience, message_id: 'VOICE_STATUS_1' };
  const valid = await live.request('post_status', 'shop', { ...base, expected_sha256: createHash('sha256').update(monoOpus).digest('hex') });
  assert.deepEqual(valid.result, { message_id: base.message_id, status: 'accepted' });
  const invalid = { ...base, message_id: 'VOICE_STATUS_2' };
  const stereoOpus = Buffer.from(monoOpus);
  stereoOpus[37] = 2;
  await writeFile(voice, stereoOpus);
  const rejected = await live.request('post_status', 'shop', invalid);
  assert.equal(rejected.code, 'invalid');
  assert.match(rejected.error, /mono Ogg Opus/);
  // A corrected file can use the same wire ID because the invalid file never reached the socket.
  await writeFile(voice, monoOpus);
  assert.equal((await live.request('post_status', 'shop', invalid)).result.status, 'accepted');
  assert.deepEqual(await readdir(path.join(temp, 'profile', 'scratch')), []);
});
