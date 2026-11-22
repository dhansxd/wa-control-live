"""Capture sidecar PCM without inference and compare it with the sidecar WAV."""
import argparse
import asyncio
import hashlib
import json
import math
import struct
import time
import wave
from pathlib import Path

from aiohttp import web

from call_adapter import create_app


class DiagnosticCall:
    def __init__(self, path, send, *, max_seconds=180):
        if not math.isfinite(max_seconds) or not 0 < max_seconds <= 180:
            raise ValueError('Diagnostic duration must be between 0 and 180 seconds')
        self.path = Path(path)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.chmod(0o700)
        self.max_seconds = max_seconds
        self.failed = asyncio.Event()
        self.report = {'mode':'audio-diagnostic', 'inference':'disabled', 'cleanup_errors':[]}
        self.recording = self.envelopes = self.deadline = None
        self.started_ns = None
        self.samples = self.frames = 0
        self.digest = hashlib.sha256()
        self.closed = False

    async def start(self, setup):
        self.started_ns = time.monotonic_ns()
        self.report.update(started_at_unix=time.time(), start_monotonic_ns=self.started_ns)
        self.recording = wave.open(str(self.path/'caller.wav'), 'wb')
        (self.path/'caller.wav').chmod(0o600)
        self.recording.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        self.envelopes = (self.path/'envelopes.ndjson').open('x')
        (self.path/'envelopes.ndjson').chmod(0o600)
        self.deadline = asyncio.get_running_loop().call_later(self.max_seconds, self.expire)

    def expire(self):
        self.report['limit_reached'] = 'wall_time'
        self.failed.set()

    def feed(self, pcm):
        if self.closed or self.failed.is_set():
            return
        if not pcm or len(pcm) % 2:
            raise ValueError('Expected nonempty PCM16 samples')
        sample_count = len(pcm)//2
        if self.samples + sample_count > int(self.max_seconds*16000):
            self.report['limit_reached'] = 'sample_count'
            self.failed.set()
            return
        now_ns = time.monotonic_ns()
        values = struct.unpack('<' + 'h'*sample_count, pcm)
        row = {
            'received_monotonic_ns':now_ns,
            'elapsed_ms':(now_ns-self.started_ns)/1_000_000,
            'sample_offset':self.samples,
            'samples':sample_count,
            'sha256':hashlib.sha256(pcm).hexdigest(),
            'peak':max(abs(value) for value in values),
            'rms':math.sqrt(sum(value*value for value in values)/sample_count),
            'zero_samples':values.count(0),
        }
        self.recording.writeframesraw(pcm)
        self.envelopes.write(json.dumps(row)+'\n')
        self.digest.update(pcm)
        self.samples += sample_count
        self.frames += 1

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.deadline is not None:
            self.deadline.cancel()
        for resource in (self.recording, self.envelopes):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    self.report['cleanup_errors'].append(f'{type(exc).__name__}: {exc}')
        self.report.update(
            samples=self.samples, input_bytes=self.samples*2, envelopes=self.frames,
            pcm_seconds=self.samples/16000, sha256=self.digest.hexdigest(),
            elapsed_ms=(time.monotonic_ns()-self.started_ns)/1_000_000 if self.started_ns else 0,
        )
        self.report['state'] = 'failed' if self.report.get('error') or self.report['cleanup_errors'] else 'captured'
        report_file = self.path/'report.json'
        report_file.write_text(json.dumps(self.report, indent=2)+'\n')
        report_file.chmod(0o600)
        if self.report['cleanup_errors']:
            raise RuntimeError('Diagnostic recording cleanup failed')


def create_probe_app(token, runs):
    return create_app(token, runs, voice_factory=DiagnosticCall, route='audio-diagnostic')


def read_pcm(path):
    with wave.open(str(path), 'rb') as recording:
        if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate(), recording.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError(f'{path} must be mono PCM16 at 16000 Hz')
        expected_bytes = recording.getnframes()*2
        pcm = recording.readframes(recording.getnframes())
    if len(pcm) != expected_bytes:
        raise ValueError(f'{path} contains truncated PCM')
    return pcm


def analyze(sidecar_wav, facade_wav):
    sidecar = read_pcm(sidecar_wav)
    facade = read_pcm(facade_wav)
    common_bytes = min(len(sidecar), len(facade))
    first_difference = next((index for index in range(common_bytes) if sidecar[index] != facade[index]), common_bytes)
    if not sidecar or not facade:
        comparison = 'empty_capture'
    elif first_difference < common_bytes:
        comparison = 'mismatch'
    elif len(sidecar) == len(facade):
        comparison = 'exact'
    elif len(sidecar) > len(facade):
        comparison = 'facade_is_prefix'
    else:
        comparison = 'sidecar_is_prefix'
    return {
        'scope':'post-decode sidecar PCM to facade PCM',
        'comparison':comparison,
        'bridge_pcm_equal':comparison == 'exact',
        'sidecar_samples':len(sidecar)//2,
        'facade_samples':len(facade)//2,
        'matching_prefix_samples':first_difference//2,
        'sidecar_sha256':hashlib.sha256(sidecar).hexdigest(),
        'facade_sha256':hashlib.sha256(facade).hexdigest(),
        'sidecar_extra_tail_samples':(len(sidecar)-len(facade))//2 if comparison == 'facade_is_prefix' else 0,
        'facade_extra_tail_samples':(len(facade)-len(sidecar))//2 if comparison == 'sidecar_is_prefix' else 0,
        'first_mismatch_sample':first_difference//2 if comparison == 'mismatch' else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('serve')
    capture.add_argument('--token-file', required=True, type=Path)
    capture.add_argument('--runs', required=True, type=Path)
    capture.add_argument('--port', type=int, default=8797)
    compare = commands.add_parser('analyze')
    compare.add_argument('sidecar_wav', type=Path)
    compare.add_argument('facade_wav', type=Path)
    args = parser.parse_args()
    if args.command == 'analyze':
        print(json.dumps(analyze(args.sidecar_wav, args.facade_wav), indent=2))
    else:
        app = create_probe_app(args.token_file.read_text().strip(), args.runs)
        web.run_app(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
