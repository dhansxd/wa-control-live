"""Default launcher pins the sidecar with inbound relay failover."""
import hashlib
import json


def test_default_preparation_selects_current_sidecar(tmp_path):
    import launch
    identity = tmp_path / 'identity.local.json'
    identity.write_text(json.dumps({'bot_number': '628111111111', 'owner_number': '628222222222'}))
    fixture = b'local-selection-fixture-not-an-executable'
    digest = hashlib.sha256(fixture).hexdigest()
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    for name in ('hermes-wa-voice_opus-media11_darwin_arm64', 'hermes-wa-voice_opus-media12_darwin_arm64'):
        (binaries / name).write_bytes(fixture)
    result = launch.prepare(tmp_path, identity, expected_sha256=digest)
    assert result['binary'] == str(binaries / 'hermes-wa-voice_opus-media12_darwin_arm64')
    assert '79c5bf9c04478bf66ff97197d050c5b54d62793871c9c5301272a57774df3502' in launch.PROFILE_RELEASE_SHA256
    assert '152771cef08f350a6b14c5ec409d7bf375757e99828c5ca54573e20c7cdd1d88' in launch.PROFILE_RELEASE_SHA256
    assert launch.RELEASE_SHA256 == 'dd41378d2cc15d3ea40b1c2c19fc27c4974fdde0ca47e643ade27bf7034c4c7a'
    assert 'fc9111ce3d68e774d82437c37d65e9388baf5a13c58ba50fe4a4b735dd7eec5d' in launch.PROFILE_RELEASE_SHA256
    assert '8399cd785b191853aeb7d1513f7f006b7c3a9abc683d7f822d770601a85b07e6' in launch.PROFILE_RELEASE_SHA256
    assert result['binary_sha256'] == digest
