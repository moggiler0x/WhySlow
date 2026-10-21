import pytest

from whyslow import seal

KEY = bytes(range(32))


def test_roundtrip():
    data = b"def build(main):\n    return main\n" * 100
    blob = seal.seal_bytes(data, KEY)
    assert blob.startswith(b"WSEAL1")
    assert blob != data and data not in blob  # открытого текста в блоке нет
    assert seal.open_sealed(blob, KEY) == data


def test_wrong_key_fails():
    blob = seal.seal_bytes(b"secret code", KEY)
    with pytest.raises(seal.SealError):
        seal.open_sealed(blob, bytes([1]) + KEY[1:])


def test_tamper_fails():
    blob = bytearray(seal.seal_bytes(b"secret code here", KEY))
    blob[-1] ^= 0x01  # меняем один байт шифртекста
    with pytest.raises(seal.SealError):
        seal.open_sealed(bytes(blob), KEY)


def test_not_a_block():
    with pytest.raises(seal.SealError):
        seal.open_sealed(b"random", KEY)


def test_feature_key_deterministic_and_distinct():
    seed = b"\xaa" * 32
    assert seal.feature_key(seed, "compare") == seal.feature_key(seed, "compare")
    assert seal.feature_key(seed, "compare") != seal.feature_key(seed, "pests")
    assert seal.feature_key(seed, "compare") != seal.feature_key(b"\xbb" * 32, "compare")
    assert len(seal.feature_key(seed, "compare")) == 32


def test_hkdf_rfc5869_vector():
    # RFC 5869, тест 1
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = seal._hkdf(ikm, salt, info, 42)
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
                         "34007208d5b887185865")
