"""Offline transport/receipt/voice comparison. Run once per archived plugin commit and HTTP result."""
import asyncio
import base64
from contextlib import asynccontextmanager, closing
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

root, case_root = map(Path, sys.argv[1:3])
http_status = int(sys.argv[3])
sys.path[:0] = [str(root), str(root / 'spikes/003-whatsapp-call')]
from gateway.config import PlatformConfig
from gateway.delivery_ledger import _initialize_schema, compute_obligation_id
from hermes_state import SessionDB
import call_adapter
import live_agent
from test_live_agent import FakeGateway, add_dm_work
from whatsapp_gateway import WhatsAppLiveAdapter

# Fictional NANP 555-01xx destinations. Transport is mocked.
owner, bot = ('1202555' + line for line in ('0100', '0101'))
sid, adm, sesskey, reference = 'session-a', 'a'*32, 'synthetic-wa', 'inbound-test'
claim = 'Gambar sudah kukirim ke WhatsApp kamu.'
caption_response = claim
case_root.mkdir(parents=True, exist_ok=True)
home = case_root / 'profile'
home.mkdir(exist_ok=True)
os.environ['HERMES_HOME'] = str(home)
picture = case_root / 'image.png'
picture.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0Z0AAAAASUVORK5CYII='))
route = owner + '@s.whatsapp.net'

class FakeHTTP:
    @asynccontextmanager
    async def post(self, url, *, json, **options):
        assert url == '/messages' and json['kind'] == 'image' and json['chat_id'] == route
        assert Path(json['path']).read_bytes() == picture.read_bytes()
        self.posts += 1
        class Response:
            status = http_status
            async def json(self):
                return {'status': 'accepted' if self.status == 200 else 'rejected',
                        'message_id': json['message_id'], 'rejection_status': 400}
        yield Response()
    posts = 0

async def execute():
    adapter = WhatsAppLiveAdapter(PlatformConfig(), owner, bot, home / 'plugin-data/unified/adapter.token')
    adapter.token_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.http, adapter.verified = FakeHTTP(), True
    transported = await adapter.send_image_file(owner, str(picture))
    assert adapter.http.posts == 1 and transported.success == (http_status == 200), (adapter.http.posts,transported)
    # Separate fact: the caption text has a matched, delivered obligation in this fixture.
    with closing(SessionDB(home / 'state.db')) as db:
        db.create_session(sid, 'whatsapp', session_key=sesskey, chat_type='dm', chat_id=route, user_id=owner)
        payload = {'text': 'Buat gambar lalu kirim ke DM.', 'native_text_v1': {
            'source': {'platform': 'whatsapp', 'chat_type': 'dm', 'chat_id': route, 'user_id': owner},
            'event': {'message_id': reference}}}
        add_dm_work(db, sid, adm, payload, status='terminal', outcome='completed')
        with db._conn as conn:
            _initialize_schema(conn)
            conn.execute("""INSERT INTO delivery_obligations
                (obligation_id, session_key, platform, chat_id, content, state, created_at, updated_at)
                VALUES (?, ?, 'whatsapp', ?, ?, 'delivered', 0, 0)""",
                (compute_obligation_id(sesskey, reference, caption_response), sesskey, route, caption_response))
    gateway = FakeGateway()
    gateway.admissions['fixture'] = {'admission_id': adm, 'ref': {'session_id': sid}}
    gateway.receipt['result']['final_response'] = claim + '\nMEDIA:' + str(picture)
    observed = []
    class Jobs:
        async def observe_work(self, session_id, admission_id):
            result = await live_agent._run(home, owner, {'action': 'observe_work', 'session_id': session_id,
                'admission_id': admission_id}, resolve=gateway.resolve, connect=gateway.connect)
            observed.append(result)
            return result
    packets = []
    class Capture:
        def send(self, raw):
            payload = json.loads(raw)
            assert payload['type'] == 'delegation.context.append'
            packets.append(payload)
            asyncio.create_task(voice.on_data({'type':'delegation.context.appended',
                'delegation_item_id': payload['delegation_item_id']}))
    async def send_sidecar(_event): pass
    with patch.object(call_adapter, 'codex_command', lambda: ['offline-only']):
        voice = call_adapter.LiveCall(case_root / 'live', send_sidecar, agent_jobs=Jobs())
        voice.connection.channel = Capture()
        voice.hermes_session_id = sid
        voice.current_spoken_turn = 'caller-1'
        try:
            voice.watch_dm_work(adm, 'request-1', 'caller-1')
            await asyncio.wait_for(voice.work_followers[adm][0], timeout=3)
        finally:
            await voice.close()
    assert len(observed) == len(packets) == 1
    obs, packet = observed[0], packets[0]
    assert obs['status'] == 'completed' and obs['text_delivery'] == 'sent' and obs['has_attachments']
    assert packet['channel'] == 'speakable' and claim in packet['content'][0]['text']
    print(json.dumps({'version': root.name, 'http_status_fixture':http_status,
        'native_media_send_success': transported.success, 'native_media_error':transported.error,
        'native_prompt_outcome_fixture':gateway.receipt['outcome'], 'observed_work':obs,
        'explicit_delivery_reply_context':call_adapter.dm_delivery_context(obs),
        'captured_provider_handoff':packet, 'observed_rpcs':[name for name,_ in gateway.calls]},
        ensure_ascii=False, indent=2))
asyncio.run(execute())
