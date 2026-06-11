"""Decoder del protocollo radio FLARM "Legacy" (v6/v7) per ricezione SDR.

Le costanti di cifratura, il syncword, il tipo di CRC e i layout dei pacchetti
v6/v7 sono presi *verbatim* dal progetto SoftRF (GPLv3, lyusupov/SoftRF,
src/protocol/radio/Legacy.cpp) e dalla relativa reverse-engineering del
protocollo FLARM. Questo modulo riporta quella logica in Python e aggiunge un
front-end DSP (demodulazione 2-FSK/GFSK + Manchester + sync + CRC) per estrarre
i pacchetti dai campioni I/Q di un PlutoSDR.

PHY (da legacy_proto_desc di SoftRF):
  - 2-FSK, 100 kchip/s, deviazione +/-50 kHz
  - whitening Manchester (bit 1 -> "01", bit 0 -> "10", MSB-first)
  - payload invertito (RF_PAYLOAD_INVERTED)
  - syncword on-air (8 byte) = Manchester(0xF531FAB6)
  - payload 24 byte + CRC CCITT init 0xFFFF (2 byte)

NOTA: la decodifica v6/v7 richiede una posizione di riferimento (la stazione)
per ricostruire lat/lon "relative", e l'orario UTC corrente per la chiave
(v6 cambia ogni 64 s, v7 ogni 16 s). La validazione end-to-end senza hardware
e' fatta via self-test (encode -> modula -> demodula -> decode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# --- Costanti protocollo (verbatim da SoftRF Legacy.h) -----------------------

DELTA = 0x9E3779B9
LEGACY_KEY1 = (
    0xE43276DF, 0xDCA83759, 0x9802B8AC, 0x4675A56B,
    0xFC78EA65, 0x804B90EA, 0xB76542CD, 0x329DFA32,
)
LEGACY_KEY2 = 0x045D9F3B
LEGACY_KEY3 = 0x87B562F4
LEGACY_KEY4 = 0x956F6C77
LEGACY_KEY5 = (0xA5F9B21C, 0xAB3F9D12, 0xC6F34E34, 0xD72FA378)

# Syncword on-air (gia' codificato Manchester). Vedi commento in Legacy.h:
#   IEEE Manchester(F531FAB6) = 55 99 A5 A9 55 66 65 96
LEGACY_SYNCWORD = bytes((0x55, 0x99, 0xA5, 0xA9, 0x55, 0x66, 0x65, 0x96))
LEGACY_PAYLOAD_SIZE = 24
LEGACY_CRC_SIZE = 2

CHIP_RATE_HZ = 100_000
FSK_DEVIATION_HZ = 50_000

_MASK32 = 0xFFFFFFFF
MPS_PER_KNOT = 0.514444
FEET_PER_METER = 3.2808399

LON_DIV_TABLE = (
    53, 53, 54, 54, 55, 55,
    56, 56, 57, 57, 58, 58, 59, 59, 60, 60,
    61, 61, 62, 62, 63, 63, 64, 64, 65, 65,
    67, 68, 70, 71, 73, 74, 76, 77, 79, 80,
    82, 83, 85, 86, 88, 89, 91, 94, 98, 101,
    105, 108, 112, 115, 119, 122, 126, 129, 137, 144,
    152, 159, 167, 174, 190, 205, 221, 236, 252,
    267, 299, 330, 362, 425, 489, 552, 616, 679, 743, 806, 806,
)


# --- Cifratura XXTEA/btea e derivazione chiavi (verbatim da Legacy.cpp) -------

def btea(v: list[int], n: int, key: tuple[int, ...] | list[int]) -> None:
    """XXTEA in-place su una lista di uint32. n>1 cifra, n<-1 decifra."""
    def mx() -> int:
        return (((z >> 5 ^ (y << 2) & _MASK32) + (y >> 3 ^ (z << 4) & _MASK32)) & _MASK32) ^ \
               (((sum_ ^ y) + (key[(p & 3) ^ e] ^ z)) & _MASK32)

    if n > 1:
        rounds = 6
        sum_ = 0
        z = v[n - 1]
        while True:
            sum_ = (sum_ + DELTA) & _MASK32
            e = (sum_ >> 2) & 3
            for p in range(n - 1):
                y = v[p + 1]
                z = v[p] = (v[p] + mx()) & _MASK32
            p = n - 1
            y = v[0]
            z = v[n - 1] = (v[n - 1] + mx()) & _MASK32
            rounds -= 1
            if rounds == 0:
                break
    elif n < -1:
        n = -n
        rounds = 6
        sum_ = (rounds * DELTA) & _MASK32
        y = v[0]
        while True:
            e = (sum_ >> 2) & 3
            for p in range(n - 1, 0, -1):
                z = v[p - 1]
                y = v[p] = (v[p] - mx()) & _MASK32
            p = 0
            z = v[n - 1]
            y = v[0] = (v[0] - mx()) & _MASK32
            sum_ = (sum_ - DELTA) & _MASK32
            rounds -= 1
            if rounds == 0:
                break


def obscure(key: int, seed: int) -> int:
    m1 = (seed * (key ^ (key >> 16))) & _MASK32
    m2 = (seed * (m1 ^ (m1 >> 16))) & _MASK32
    return (m2 ^ (m2 >> 16)) & _MASK32


def make_v6_key(timestamp: int, address: int) -> list[int]:
    key = [0, 0, 0, 0]
    for i in range(4):
        ndx = i + 4 if ((timestamp >> 23) & 1) else i
        key[i] = obscure(LEGACY_KEY1[ndx] ^ (((timestamp >> 6) ^ address) & _MASK32), LEGACY_KEY2) ^ LEGACY_KEY3
        key[i] &= _MASK32
    return key


def make_v7_key(key: list[int]) -> None:
    """Mescola in-place i 16 byte di key[4] (verbatim da make_v7_key)."""
    bkeys = bytearray()
    for w in key:
        bkeys += int(w & _MASK32).to_bytes(4, "little")
    x = bkeys[15]
    sum_ = 0
    for _q in range(2):
        sum_ = (sum_ + DELTA) & _MASK32
        for p in range(16):
            z = x & 0xFF
            y = bkeys[(p + 1) % 16]
            x = bkeys[p]
            x = (x + (((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ (sum_ ^ y)))) & _MASK32
            bkeys[p] = x & 0xFF
    for i in range(4):
        key[i] = int.from_bytes(bkeys[4 * i:4 * i + 4], "little")


# --- CRC CCITT (init 0xFFFF, poly 0x1021) ------------------------------------

def crc_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# --- enscale/descale (verbatim da Legacy.cpp) --------------------------------

def descale(value: int, mbits: int, ebits: int) -> int:
    offset = 1 << mbits
    signbit = offset << ebits
    negative = value & signbit
    value &= (signbit - 1)
    if value >= offset:
        exp = value >> mbits
        value &= (offset - 1)
        value += offset
        value <<= exp
        value -= offset
    return -value if negative else value


def enscale_signed(value: int, mbits: int, ebits: int) -> int:
    offset = 1 << mbits
    signbit = offset << ebits
    max_val = signbit - 1
    sign = 0
    if value < 0:
        value = -value
        sign = signbit
    if value >= offset:
        e = 0
        m = offset + value
        mlimit = offset + offset - 1
        while m > mlimit:
            m >>= 1
            e += offset
            if e > max_val:
                return sign | max_val
        m -= offset
        return sign | e | m
    return sign | value


def enscale_unsigned(value: int, mbits: int, ebits: int) -> int:
    offset = 1 << mbits
    max_val = (offset << ebits) - 1
    if value >= offset:
        e = 0
        m = offset + value
        mlimit = offset + offset - 1
        while m > mlimit:
            m >>= 1
            e += offset
            if e > max_val:
                return max_val
        m -= offset
        return e | m
    return value


def _parity(byte: int) -> int:
    return bin(byte & 0xFF).count("1") & 1


# --- Lettura/scrittura campi bitfield (GCC little-endian, packed) -------------

class _BitReader:
    def __init__(self, raw: bytes) -> None:
        self.value = int.from_bytes(raw, "little")
        self.pos = 0

    def take(self, bits: int, signed: bool = False) -> int:
        out = (self.value >> self.pos) & ((1 << bits) - 1)
        self.pos += bits
        if signed and (out & (1 << (bits - 1))):
            out -= (1 << bits)
        return out


def _words_from_bytes(buf: bytes) -> list[int]:
    return [int.from_bytes(buf[4 * i:4 * i + 4], "little") for i in range(len(buf) // 4)]


def _bytes_from_words(words: list[int]) -> bytes:
    out = bytearray()
    for w in words:
        out += int(w & _MASK32).to_bytes(4, "little")
    return bytes(out)


@dataclass
class FlarmTarget:
    addr: int
    addr_type: int
    aircraft_type: int
    latitude: float
    longitude: float
    altitude_m: float
    speed_kt: float
    course_deg: float
    vs_ft_min: float
    stealth: bool
    no_track: bool
    version: int

    @property
    def addr_hex(self) -> str:
        return f"{self.addr & 0xFFFFFF:06X}"


# --- Decodifica pacchetto (24 byte gia' decifrati a livello di trasporto) -----

def decode_packet(payload: bytes, ref_lat: float, ref_lon: float, timestamp: int) -> Optional[FlarmTarget]:
    if len(payload) < LEGACY_PAYLOAD_SIZE:
        return None
    raw = bytes(payload[:LEGACY_PAYLOAD_SIZE])
    pkt_type = (int.from_bytes(raw[0:4], "little") >> 24) & 0xF
    if pkt_type == 0:
        return _decode_v6(raw, ref_lat, ref_lon, timestamp)
    if pkt_type == 2:
        return _decode_v7(raw, ref_lat, ref_lon, timestamp)
    return None


def decode_payload(payload: bytes, ref_lat: float, ref_lon: float, now_ts: float,
                   offsets: tuple[int, ...] = (0, -16, 16, -32, 32, -64, 64)) -> Optional[FlarmTarget]:
    """Prova alcune finestre temporali attorno all'ora corrente (la chiave v6
    cambia ogni 64 s, v7 ogni 16 s); ritorna il primo decode valido."""
    base = int(now_ts)
    for off in offsets:
        target = decode_packet(payload, ref_lat, ref_lon, base + off)
        if target is not None:
            return target
    return None


def _decode_v6(raw: bytes, ref_lat: float, ref_lon: float, timestamp: int) -> Optional[FlarmTarget]:
    words = _words_from_bytes(raw)
    addr = words[0] & 0xFFFFFF
    key = make_v6_key(timestamp, (addr << 8) & 0xFFFFFF)
    sub = words[1:6]
    btea(sub, -5, key)  # decifra words[1..5], word0 in chiaro
    words[1:6] = sub
    dec = _bytes_from_words(words)

    if sum(_parity(b) for b in dec) % 2:
        return None

    r = _BitReader(dec)
    addr = r.take(24)
    _type = r.take(4)
    addr_type = r.take(3)
    r.take(1)
    vs = r.take(10, signed=True)
    r.take(2)
    r.take(1)  # airborne
    stealth = r.take(1)
    no_track = r.take(1)
    r.take(1)  # parity
    r.take(12)  # gps
    aircraft_type = r.take(4)
    lat_raw = r.take(19)
    alt = r.take(13)
    lon_raw = r.take(20)
    r.take(10)
    smult = r.take(2)
    ns = [r.take(8, signed=True) for _ in range(4)]
    ew = [r.take(8, signed=True) for _ in range(4)]

    round_lat = int(ref_lat * 1e7) >> 7
    lat = (lat_raw - round_lat) % 0x080000
    if lat >= 0x040000:
        lat -= 0x080000
    lat = (lat + round_lat) << 7

    round_lon = int(ref_lon * 1e7) >> 7
    lon = (lon_raw - round_lon) % 0x100000
    if lon >= 0x080000:
        lon -= 0x100000
    lon = (lon + round_lon) << 7

    ns_avg = sum(ns) // 4
    ew_avg = sum(ew) // 4
    speed4 = float(np.hypot(ew_avg, ns_avg)) * (1 << smult)
    course = 0.0
    if speed4 > 0:
        ang = float(np.degrees(np.arctan2(ns_avg, ew_avg)))
        course = (90.0 - ang) if ang <= 90.0 else (450.0 - ang)
    vs10 = vs << smult

    return FlarmTarget(
        addr=addr,
        addr_type=addr_type,
        aircraft_type=aircraft_type,
        latitude=lat / 1e7,
        longitude=lon / 1e7,
        altitude_m=float(alt),
        speed_kt=(speed4 / 4.0) / MPS_PER_KNOT,
        course_deg=course,
        vs_ft_min=float(vs10) * (FEET_PER_METER * 6.0),
        stealth=bool(stealth),
        no_track=bool(no_track),
        version=6,
    )


def _decode_v7(raw: bytes, ref_lat: float, ref_lon: float, timestamp: int) -> Optional[FlarmTarget]:
    words = _words_from_bytes(raw)
    tail = words[2:6]
    btea(tail, -4, LEGACY_KEY5)  # decifra words[2..5]
    key_v7 = [words[0], words[1], (timestamp >> 4) & _MASK32, LEGACY_KEY4]
    make_v7_key(key_v7)
    dec_words = [
        words[0], words[1],
        tail[0] ^ key_v7[0], tail[1] ^ key_v7[1],
        tail[2] ^ key_v7[2], tail[3] ^ key_v7[3],
    ]
    dec = _bytes_from_words(dec_words)

    r = _BitReader(dec)
    addr = r.take(24)
    _type = r.take(4)
    addr_type = r.take(3)
    r.take(1)
    r.take(22)  # _unk2
    stealth = r.take(1)
    no_track = r.take(1)
    r.take(2)
    r.take(2)
    r.take(2)
    r.take(2)
    r.take(2)  # _unk7
    _tstamp = r.take(4)
    aircraft_type = r.take(4)
    r.take(1)
    alt_raw = r.take(13)
    lat_raw = r.take(20)
    lon_raw = r.take(20)
    _turn = r.take(9, signed=True)
    hs = r.take(10)
    vs = r.take(9, signed=True)
    course = r.take(10)
    _airborne = r.take(2)

    # I 4 bit meno significativi del timestamp: filtra chiavi/tempi errati.
    if _tstamp not in ((timestamp & 0xF), ((timestamp - 1) & 0xF), ((timestamp + 1) & 0xF)):
        return None

    alt = descale(alt_raw, 12, 1) - 1000

    round_lat = int(ref_lat * 1e7) // 52
    lat = (lat_raw - round_lat) % 0x100000
    if lat >= 0x080000:
        lat -= 0x100000
    lat = (lat + round_lat) * 52
    latitude = lat / 1e7

    ilat = min(89, int(abs(latitude)))
    lon_div = 52 if ilat < 14 else LON_DIV_TABLE[ilat - 14]
    round_lon = int(ref_lon * 1e7) // lon_div
    lon = (lon_raw - round_lon) % 0x100000
    if lon >= 0x080000:
        lon -= 0x100000
    lon = (lon + round_lon) * lon_div
    longitude = lon / 1e7

    speed10 = descale(hs, 8, 2)
    vs10 = descale(vs, 6, 2)

    return FlarmTarget(
        addr=addr,
        addr_type=addr_type,
        aircraft_type=aircraft_type,
        latitude=latitude,
        longitude=longitude,
        altitude_m=float(alt),
        speed_kt=(speed10 / 10.0) / MPS_PER_KNOT,
        course_deg=course / 2.0,
        vs_ft_min=float(vs10) * (FEET_PER_METER * 6.0),
        stealth=bool(stealth),
        no_track=bool(no_track),
        version=7,
    )


# --- Front-end DSP: demodulazione 2-FSK + Manchester + sync + CRC ------------

def _sync_chip_bits() -> np.ndarray:
    """Bit (chip) del syncword, MSB-first, come +/-1."""
    bits = []
    for byte in LEGACY_SYNCWORD:
        for k in range(7, -1, -1):
            bits.append(1 if (byte >> k) & 1 else -1)
    return np.array(bits, dtype=np.float32)


_SYNC_CHIPS = _sync_chip_bits()


def _manchester_decode_bits(chips: np.ndarray) -> Optional[list[int]]:
    """chips: array di 0/1; coppia (1,0)->bit0, (0,1)->bit1 (IEEE 1=01)."""
    if len(chips) % 2:
        chips = chips[:-1]
    bits = []
    for i in range(0, len(chips), 2):
        a, b = chips[i], chips[i + 1]
        if a == 0 and b == 1:
            bits.append(1)
        elif a == 1 and b == 0:
            bits.append(0)
        else:
            return None  # violazione Manchester
    return bits


def _bytes_from_msb_bits(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for k in range(8):
            byte = (byte << 1) | bits[i + k]
        out.append(byte)
    return bytes(out)


@dataclass
class FlarmDemodResult:
    payloads: list[bytes] = field(default_factory=list)
    crc_ok: int = 0
    crc_bad: int = 0
    sync_hits: int = 0


class FlarmLegacyReceiver:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.sps = max(2, int(round(self.sample_rate / CHIP_RATE_HZ)))
        # Template del sync upsampled (per la cross-correlazione sul discriminatore).
        self.sync_template = np.repeat(_SYNC_CHIPS, self.sps).astype(np.float32)
        self.sync_template -= self.sync_template.mean()
        # chip totali per pacchetto: (payload+crc) byte * 8 bit * 2 chip Manchester
        self.frame_chips = (LEGACY_PAYLOAD_SIZE + LEGACY_CRC_SIZE) * 8 * 2

    def _discriminator(self, iq: np.ndarray) -> np.ndarray:
        x = np.asarray(iq, dtype=np.complex64)
        if x.size < 2:
            return np.zeros(0, dtype=np.float32)
        d = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
        return d

    def _find_sync(self, disc: np.ndarray) -> list[tuple[int, float]]:
        """Ritorna [(indice_inizio_chip, polarita)] per ogni picco di correlazione.

        Cross-correlazione lineare via FFT: corr[lag] = somma_n disc[lag+n]*tmpl[n],
        quindi il picco a `lag` e' l'indice di inizio del syncword nel discriminatore.
        """
        tmpl = self.sync_template
        sync_len = tmpl.size
        frame_len = self.frame_chips * self.sps
        valid = disc.size - sync_len - frame_len
        if valid <= 0:
            return []
        n = disc.size
        size = 1
        while size < n + sync_len:
            size <<= 1
        fd = np.fft.rfft(disc, size)
        ft = np.fft.rfft(tmpl, size)
        corr = np.fft.irfft(fd * np.conj(ft), size)[: valid]
        mag = np.abs(corr)
        peak = float(np.max(mag))
        noise = float(np.median(mag)) + 1e-9
        # Gate sul guadagno del filtro adattato: un vero burst supera di molto il rumore.
        if peak < 8.0 * noise:
            return []
        thresh = 0.5 * peak
        candidates = np.flatnonzero(mag >= thresh)
        if candidates.size == 0:
            return []
        accepted: list[int] = []
        hits: list[tuple[int, float]] = []
        # Sopprime i lobi laterali: un solo picco per pacchetto (distanza >= frame).
        for lag in candidates[np.argsort(mag[candidates])[::-1]]:
            lag = int(lag)
            if any(abs(lag - a) < frame_len for a in accepted):
                continue
            accepted.append(lag)
            polarity = 1.0 if corr[lag] > 0 else -1.0
            hits.append((lag + sync_len, polarity))
            if len(hits) >= 32:
                break
        return hits

    def _slice_chips(self, disc: np.ndarray, start: int, count: int, polarity: float) -> Optional[np.ndarray]:
        end = start + count * self.sps
        if end > disc.size:
            return None
        # Integra ogni chip sulla parte centrale della sua finestra (robusto al rumore/timing).
        window = disc[start:end].reshape(count, self.sps) * polarity
        # Integra quasi tutto il chip (escludendo i bordi con i transitori) per max SNR.
        lo = max(2, self.sps // 10)
        hi = self.sps - lo
        scores = window[:, lo:hi].mean(axis=1)
        return (scores > 0).astype(np.int8)

    def _bursts(self, mag: np.ndarray) -> list[tuple[int, int]]:
        """Rilevatore d'energia: ritorna (start, end) delle regioni ad alta ampiezza.

        Il discriminatore FM del rumore a bassa ampiezza produce valori enormi
        (angolo uniforme +/-pi) mentre il segnale FSK e' a ~+/-0.08 rad: bisogna
        quindi demodulare solo dentro il burst, dove l'SNR e' alto.
        """
        noise = float(np.median(mag))
        mad = float(np.median(np.abs(mag - noise))) + 1e-9
        thr = max(noise + 6.0 * mad, noise * 3.0, 1e-6)
        active = mag > thr
        if not active.any():
            return []
        padded = np.concatenate(([0], active.view(np.int8), [0]))
        edges = np.diff(padded)
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        min_active = (8 * 8 * 2) * self.sps  # almeno ~8 byte di frame attivo
        return [(int(s), int(e)) for s, e in zip(starts, ends) if (e - s) >= min_active]

    def _try_decode(self, disc: np.ndarray, result: FlarmDemodResult) -> None:
        for sync_end, polarity in self._find_sync(disc):
            result.sync_hits += 1
            chips = self._slice_chips(disc, sync_end, self.frame_chips, polarity)
            if chips is None:
                continue
            bits = _manchester_decode_bits(chips)
            if bits is None:
                continue
            data = bytearray(_bytes_from_msb_bits(bits))
            # Payload invertito on-air: de-inverti payload + CRC.
            for i in range(len(data)):
                data[i] ^= 0xFF
            if len(data) < LEGACY_PAYLOAD_SIZE + LEGACY_CRC_SIZE:
                continue
            payload = bytes(data[:LEGACY_PAYLOAD_SIZE])
            crc_rx = (data[LEGACY_PAYLOAD_SIZE] << 8) | data[LEGACY_PAYLOAD_SIZE + 1]
            if crc_ccitt(payload) == crc_rx:
                result.crc_ok += 1
                result.payloads.append(payload)
            else:
                result.crc_bad += 1

    def process(self, iq: np.ndarray) -> FlarmDemodResult:
        result = FlarmDemodResult()
        x = np.asarray(iq, dtype=np.complex64)
        frame_len = self.frame_chips * self.sps
        if x.size < self.sync_template.size + frame_len + 2 * self.sps:
            return result
        mag = np.abs(x).astype(np.float32, copy=False)
        # Demodula solo dentro ogni burst rilevato per ampiezza (SNR alto).
        needed = (16 + 64) * self.sps + frame_len + 8 * self.sps  # preambolo+sync+frame+margine
        for start, _end in self._bursts(mag)[:32]:
            a = max(0, start - 4 * self.sps)
            b = min(x.size, start + needed)
            disc = self._discriminator(x[a:b])
            if disc.size:
                self._try_decode(disc, result)
        return result


# --- Encoder/modulatore (solo per self-test, NON usato in ricezione) ---------

def _chips_from_payload(payload: bytes) -> np.ndarray:
    crc = crc_ccitt(payload)
    framed = bytearray(payload) + bytes((crc >> 8, crc & 0xFF))
    for i in range(len(framed)):
        framed[i] ^= 0xFF  # payload invertito
    chips = []
    for byte in framed:
        for k in range(7, -1, -1):
            bit = (byte >> k) & 1
            chips.extend((0, 1) if bit else (1, 0))  # Manchester IEEE 1->01
    sync_chips = [1 if c > 0 else 0 for c in _SYNC_CHIPS]
    preamble = [0, 1] * 8
    return np.array(preamble + sync_chips + chips, dtype=np.int8)


def modulate(payload: bytes, sample_rate: int, noise_amp: float = 0.0,
             pad: int = 256) -> np.ndarray:
    sps = max(2, int(round(sample_rate / CHIP_RATE_HZ)))
    chips = _chips_from_payload(payload)
    symbols = np.where(np.repeat(chips, sps) > 0, 1.0, -1.0).astype(np.float64)
    dphi = 2.0 * np.pi * FSK_DEVIATION_HZ / sample_rate
    phase = np.cumsum(symbols * dphi)
    sig = np.exp(1j * phase).astype(np.complex64)
    out = np.zeros(sig.size + 2 * pad, dtype=np.complex64)
    out[pad:pad + sig.size] = sig
    if noise_amp > 0:
        rng = np.random.default_rng(12345)
        out += (noise_amp * (rng.standard_normal(out.size) + 1j * rng.standard_normal(out.size))).astype(np.complex64)
    return out


def encode_v6_payload(addr: int, lat: float, lon: float, alt_m: int,
                      speed_kt: float, course_deg: float, vs_ft_min: float,
                      aircraft_type: int, timestamp: int) -> bytes:
    """Costruisce e cifra un pacchetto v6 (mirror di legacy_v6_encode)."""
    speedf = speed_kt * MPS_PER_KNOT
    vsf = vs_ft_min / (FEET_PER_METER * 60.0)
    speed4 = min(0x3FF, int(round(speedf * 4.0)))
    if speed4 & 0x200:
        smult = 3
    elif speed4 & 0x100:
        smult = 2
    elif speed4 & 0x080:
        smult = 1
    else:
        smult = 0
    speed = speed4 >> smult
    ns = int(speed * np.cos(np.radians(course_deg))) & 0xFF
    ew = int(speed * np.sin(np.radians(course_deg))) & 0xFF
    vs10 = int(round(vsf * 10.0))
    vs = (vs10 >> smult) & 0x3FF

    lat_i = int(lat * 1e7)
    lon_i = int(lon * 1e7)
    lat_field = (((lat_i >> 7) + (1 if (lat_i & 0x40 and lat_i >= 0) else (-1 if (lat_i & 0x40) else 0)))) & 0x7FFFF
    lon_field = (((lon_i >> 7) + (1 if (lon_i & 0x40 and lon_i >= 0) else (-1 if (lon_i & 0x40) else 0)))) & 0xFFFFF
    alt_field = (0 if alt_m < 0 else alt_m) & 0x1FFF

    # Compone i bitfield LSB-first (GCC LE packed).
    val = 0
    pos = 0

    def put(v: int, bits: int) -> None:
        nonlocal val, pos
        val |= (v & ((1 << bits) - 1)) << pos
        pos += bits

    put(addr & 0xFFFFFF, 24)
    put(0, 4)            # type = 0 (Air V6)
    put(2, 3)            # addr_type = FLARM
    put(0, 1)
    put(vs, 10)
    put(0, 2)
    put(1, 1)            # airborne
    put(0, 1)            # stealth
    put(0, 1)            # no_track
    put(0, 1)            # parity (calcolata dopo)
    put(323, 12)         # gps
    put(aircraft_type & 0xF, 4)
    put(lat_field, 19)
    put(alt_field, 13)
    put(lon_field, 20)
    put(0, 10)
    put(smult, 2)
    for _ in range(4):
        put(ns, 8)
    for _ in range(4):
        put(ew, 8)

    raw = bytearray(val.to_bytes(24, "little"))
    # parity bit: bit 15 del word1 (offset bit 32+15 = 47)
    if sum(_parity(b) for b in raw) % 2:
        raw[47 // 8] ^= 1 << (47 % 8)

    words = _words_from_bytes(bytes(raw))
    sub = words[1:6]
    btea(sub, 5, make_v6_key(timestamp, (addr << 8) & 0xFFFFFF))
    words[1:6] = sub
    return _bytes_from_words(words)


# --- Self-test ---------------------------------------------------------------

def self_test() -> None:
    # 1) btea round-trip
    key = (0x01234567, 0x89ABCDEF, 0xDEADBEEF, 0x0BADF00D)
    data = [0x11111111, 0x22222222, 0x33333333, 0x44444444, 0x55555555, 0x66666666]
    orig = list(data)
    btea(data, 5, key)
    assert data != orig
    btea(data, -5, key)
    assert data == orig, "btea round-trip fallito"

    # 2) CRC: appendendo il CRC, il ricalcolo sull'intero frame deve azzerarsi
    msg = bytes(range(24))
    crc = crc_ccitt(msg)
    assert crc_ccitt(msg + bytes((crc >> 8, crc & 0xFF))) == 0, "CRC CCITT incoerente"

    # 3) descale/enscale round-trip
    for v in (0, 5, 100, 1000, -7, -250):
        assert descale(enscale_signed(v, 6, 2), 6, 2) == v or abs(v) >= (1 << 6), "enscale/descale"

    # 4) v6 end-to-end: payload -> cifra -> modula IQ -> demodula -> decifra -> parse
    sample_rate = 4_000_000
    ref_lat, ref_lon, ts = 45.80, 9.10, 1_700_000_000
    addr = 0x3FC1A2
    payload = encode_v6_payload(
        addr=addr, lat=45.812345, lon=9.083210, alt_m=950,
        speed_kt=78.0, course_deg=270.0, vs_ft_min=-180.0,
        aircraft_type=1, timestamp=ts,
    )
    # Buffer realistico: burst immerso nel rumore di fondo (come in ricezione).
    burst = modulate(payload, sample_rate, noise_amp=0.05)
    rng = np.random.default_rng(2024)
    iq = (0.03 * (rng.standard_normal(200000) + 1j * rng.standard_normal(200000))).astype(np.complex64)
    off = 60000
    iq[off:off + burst.size] += burst
    rx = FlarmLegacyReceiver(sample_rate)
    res = rx.process(iq)
    assert res.payloads, f"nessun pacchetto demodulato (sync_hits={res.sync_hits}, crc_bad={res.crc_bad})"
    target = decode_packet(res.payloads[0], ref_lat, ref_lon, ts)
    assert target is not None and target.version == 6, "decode v6 fallito"
    assert (target.addr & 0xFFFFFF) == addr, f"addr {target.addr_hex} != {addr:06X}"
    assert abs(target.latitude - 45.812345) < 0.001, f"lat {target.latitude}"
    assert abs(target.longitude - 9.083210) < 0.001, f"lon {target.longitude}"
    assert abs(target.altitude_m - 950) <= 1, f"alt {target.altitude_m}"

    # 5) v7 crypto+parse round-trip (mirror del percorso di decodifica v7)
    addr7 = 0x1A2B3C
    plain = [
        (addr7 & 0xFFFFFF) | (2 << 24) | (2 << 28),  # word0: addr, type=2, addr_type=FLARM
        0, 0, 0, 0, 0,
    ]
    key_v7 = [plain[0], plain[1], (ts >> 4) & _MASK32, LEGACY_KEY4]
    make_v7_key(key_v7)
    enc_tail = [plain[2] ^ key_v7[0], plain[3] ^ key_v7[1], plain[4] ^ key_v7[2], plain[5] ^ key_v7[3]]
    btea(enc_tail, 4, LEGACY_KEY5)
    v7_raw = _bytes_from_words([plain[0], plain[1], *enc_tail])
    t7 = decode_packet(v7_raw, ref_lat, ref_lon, ts)
    assert t7 is not None and t7.version == 7, "decode v7 fallito"
    assert (t7.addr & 0xFFFFFF) == addr7, f"v7 addr {t7.addr_hex} != {addr7:06X}"

    print("Self-test FLARM legacy OK "
          f"(v6 addr={target.addr_hex} lat={target.latitude:.5f} lon={target.longitude:.5f} "
          f"alt={target.altitude_m:.0f}m spd={target.speed_kt:.1f}kt | v7 addr={t7.addr_hex})")


if __name__ == "__main__":
    self_test()
