"""Just enough H.264 (Annex B) parsing to record a stream untouched: splitting
an access unit into NAL units, and reading the picture size from its SPS."""

NAL_SPS = 7

# Profiles whose SPS carries chroma format and bit depth fields.
_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def nal_units(data: bytes) -> list[bytes]:
    """The NAL units of the Annex B bytes `data`, start codes stripped."""
    units = []
    start = None
    i = 0
    n = len(data)
    while i + 2 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start is not None:
                units.append(data[start:i].rstrip(b"\x00"))
            i += 3
            start = i
        else:
            i += 1
    if start is not None:
        units.append(data[start:])
    return [u for u in units if u]


def has_sps(data: bytes) -> bool:
    """Whether the access unit `data` contains an SPS."""
    return any(u[0] & 0x1F == NAL_SPS for u in nal_units(data))


def sps_size(data: bytes) -> tuple[int, int] | None:
    """The (width, height) in pixels of the first SPS in the access unit
    `data`, after cropping; None if it has no SPS or it can't be read."""
    for unit in nal_units(data):
        if unit[0] & 0x1F == NAL_SPS:
            try:
                return _parse_sps(_unescape(unit[1:]))
            except _EndOfData:
                return None
    return None


def _unescape(rbsp: bytes) -> bytes:
    """Removes emulation-prevention bytes (00 00 03 → 00 00)."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


class _EndOfData(Exception):
    pass


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bit(self) -> int:
        if self.pos >= len(self.data) * 8:
            raise _EndOfData
        b = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def unsigned_golomb(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise _EndOfData
        return (1 << zeros) - 1 + self.bits(zeros)

    def signed_golomb(self) -> int:
        k = self.unsigned_golomb()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _parse_sps(rbsp: bytes) -> tuple[int, int] | None:
    r = _Bits(rbsp)
    profile_idc = r.bits(8)
    r.bits(16)  # constraint flags, level_idc
    r.unsigned_golomb()  # seq_parameter_set_id
    chroma_format_idc = 1
    separate_colour_plane = 0
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = r.unsigned_golomb()
        if chroma_format_idc == 3:
            separate_colour_plane = r.bit()
        r.unsigned_golomb()  # bit_depth_luma_minus8
        r.unsigned_golomb()  # bit_depth_chroma_minus8
        r.bit()  # qpprime_y_zero_transform_bypass_flag
        if r.bit():  # seq_scaling_matrix_present_flag
            return None
    r.unsigned_golomb()  # log2_max_frame_num_minus4
    poc_type = r.unsigned_golomb()
    if poc_type == 0:
        r.unsigned_golomb()  # log2_max_pic_order_cnt_lsb_minus4
    elif poc_type == 1:
        r.bit()  # delta_pic_order_always_zero_flag
        r.signed_golomb()  # offset_for_non_ref_pic
        r.signed_golomb()  # offset_for_top_to_bottom_field
        for _ in range(r.unsigned_golomb()):
            r.signed_golomb()
    r.unsigned_golomb()  # max_num_ref_frames
    r.bit()  # gaps_in_frame_num_value_allowed_flag
    width_mbs = r.unsigned_golomb() + 1
    height_map_units = r.unsigned_golomb() + 1
    frame_mbs_only = r.bit()
    if not frame_mbs_only:
        r.bit()  # mb_adaptive_frame_field_flag
    r.bit()  # direct_8x8_inference_flag
    width = width_mbs * 16
    height = height_map_units * 16 * (2 - frame_mbs_only)
    if r.bit():  # frame_cropping_flag
        left, right, top, bottom = (
            r.unsigned_golomb(),
            r.unsigned_golomb(),
            r.unsigned_golomb(),
            r.unsigned_golomb(),
        )
        if chroma_format_idc == 0 or separate_colour_plane:
            crop_x, crop_y = 1, 2 - frame_mbs_only
        else:
            sub_w = 1 if chroma_format_idc == 3 else 2
            sub_h = 2 if chroma_format_idc == 1 else 1
            crop_x, crop_y = sub_w, sub_h * (2 - frame_mbs_only)
        width -= crop_x * (left + right)
        height -= crop_y * (top + bottom)
    if width <= 0 or height <= 0:
        return None
    return width, height
