"""One prerecorded task through GPT-Live → native Hermes → actual reply audio."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'001-codex-audio'))
from probe import run_probe
from handoff import Handoff, run_hermes

PROMPT = (
    'Kamu adalah asisten dalam panggilan WhatsApp ini. Bicara bahasa Indonesia santai, pakai aku/kamu. '
    'Ini sesi dengan backend Hermes yang benar-benar bisa membuat dan membaca file. '
    'Tunggu ucapan pengguna. Untuk pekerjaan membuat atau membaca file, delegasikan '
    'permintaan lengkap kepada backend melalui delegasi, tepat satu kali, lalu tunggu. '
    'Jangan mengarang hasil atau mengaku file dibuat sebelum hasil backend tiba. '
    'Tidak perlu sapaan atau filler. Sesudah hasil Hermes tiba, sampaikan statusnya '
    'singkat dan bacakan isi file yang benar-benar dilaporkan backend, lalu diam.'
)


async def run_task(input_path, output_dir, *, completed_store=None):
    import sqlite3
    async def execute(request):
        if completed_store is not None:
            raise RuntimeError('Recovery playback cannot execute a second Hermes task')
        return await run_hermes(request, output_dir/'hermes')
    async def delegate(event):
        # run_probe creates its fresh run directory before the first remote event.
        store = output_dir/'handoff.sqlite'
        if completed_store is not None and not store.exists():
            with sqlite3.connect(Path(completed_store).resolve().as_uri()+'?mode=ro', uri=True) as source:
                if source.execute('SELECT state FROM task WHERE id=1').fetchone() != ('completed',):
                    raise RuntimeError('Recovery requires an explicitly reconciled completed result')
                with sqlite3.connect(store) as dest:
                    source.backup(dest)
        if completed_store is not None:
            # This is explicitly playback, not a new task. Names can be transcribed
            # differently on identical audio; keep the known file/content binding.
            request = ' '.join(c.get('text', '') for c in event['item']['content']).lower()
            if not all(term in request for term in ('catatan', 'uji', 'tes suara berhasil', 'bacakan')):
                raise RuntimeError('Recovery speech does not match this known note fixture')
            with sqlite3.connect(store) as db:
                return json.loads(db.execute("SELECT result FROM task WHERE id=1 AND state='completed'").fetchone()[0])
        bridge = Handoff(store, execute)
        return await bridge.handle(event)
    return await run_probe(input_path, output_dir, timeout=210, handoff=delegate, realtime_prompt=PROMPT)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--completed-store', type=Path, help='Playback only: use a reconciled saved task; never execute again')
    args = parser.parse_args()
    result = asyncio.run(run_task(args.input.resolve(), args.output_dir.resolve(), completed_store=args.completed_store))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] != 'failed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
