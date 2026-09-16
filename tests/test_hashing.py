import hashlib

from modsync.hashing import sha256_file


def test_sha256_file(tmp_path):
    payload = b"ModSync test payload\n"
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)

    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()
