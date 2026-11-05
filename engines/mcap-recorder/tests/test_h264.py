from recorder.h264 import has_sps, nal_units, sps_size

# Real SPS NALs from x264 with the flows' settings (tune=zerolatency
# speed-preset=ultrafast). The 1080p one has an emulation-prevention byte and
# is cropped from 1088 lines.
SPS_1080 = bytes.fromhex("6742c028da01e0089f97016a02020280000003008000001e478c1950")
SPS_480 = bytes.fromhex("6742c01eda0280f6c05a8303035280000003008000001e478b1750")
AUD = bytes.fromhex("0910")
IDR_STUB = bytes.fromhex("65888400")
P_STUB = bytes.fromhex("419a00")
SC = b"\x00\x00\x00\x01"


def test_nal_units_splits_3_and_4_byte_start_codes():
    assert nal_units(SC + AUD + b"\x00\x00\x01" + SPS_480) == [AUD, SPS_480]


def test_sps_size_1080p_cropped():
    assert sps_size(SC + AUD + SC + SPS_1080 + SC + IDR_STUB) == (1920, 1080)


def test_sps_size_480p():
    assert sps_size(SC + SPS_480) == (640, 480)


def test_no_sps():
    au = SC + AUD + SC + P_STUB
    assert not has_sps(au) and sps_size(au) is None


def test_garbage_sps_returns_none():
    assert sps_size(SC + b"\x67\xff") is None
