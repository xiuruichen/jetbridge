"""Minimal protobuf codec for Google Flights' ``tfs`` search parameter.

Field layout (community-documented, verified against live URLs in recon):

    message Info {
      repeated FlightData data = 3;
      repeated Passenger passengers = 8;   // enum, one entry per traveller
      Seat seat = 9;                       // 1 economy, 2 premium, 3 business, 4 first
      Trip trip = 19;                      // 1 round trip, 2 one way, 3 multi-city
    }
    message FlightData {
      string date = 2;                     // YYYY-MM-DD
      optional int32 max_stops = 5;
      repeated string airlines = 6;
      Airport from_flight = 13;
      Airport to_flight = 14;
    }
    message Airport { string airport = 2; }

Hand-rolled (varint + length-delimited only) to avoid a protobuf dependency.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from jetbridge.models.core import Cabin

SEAT = {Cabin.ECONOMY: 1, Cabin.PREMIUM: 2, Cabin.BUSINESS: 3, Cabin.FIRST: 4}
SEAT_REV = {v: k for k, v in SEAT.items()}
ADULT = 1
ONE_WAY, ROUND_TRIP = 2, 1


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _ld(field_no: int, payload: bytes) -> bytes:
    return _key(field_no, 2) + _varint(len(payload)) + payload


def _vi(field_no: int, n: int) -> bytes:
    return _key(field_no, 0) + _varint(n)


@dataclass(frozen=True)
class Leg:
    date: str
    origin: str
    destination: str
    max_stops: int | None = None
    airlines: tuple[str, ...] = ()


@dataclass(frozen=True)
class TFS:
    legs: tuple[Leg, ...]
    adults: int = 1
    cabin: Cabin = Cabin.ECONOMY
    trip: int = ONE_WAY
    _extra: dict = field(default_factory=dict, compare=False)

    def encode(self) -> bytes:
        out = bytearray()
        for leg in self.legs:
            fd = bytearray(_ld(2, leg.date.encode()))
            if leg.max_stops is not None:
                fd += _vi(5, leg.max_stops)
            for a in leg.airlines:
                fd += _ld(6, a.encode())
            fd += _ld(13, _ld(2, leg.origin.encode()))
            fd += _ld(14, _ld(2, leg.destination.encode()))
            out += _ld(3, bytes(fd))
        for _ in range(self.adults):
            out += _vi(8, ADULT)
        out += _vi(9, SEAT[self.cabin])
        out += _vi(19, self.trip)
        return bytes(out)

    def b64(self) -> str:
        return base64.urlsafe_b64encode(self.encode()).decode().rstrip("=")

    def url(self, currency: str = "USD") -> str:
        return f"https://www.google.com/travel/flights/search?tfs={self.b64()}&curr={currency}&hl=en&gl=us"


# ---- decoding (tests + debugging captured URLs) -----------------------------


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    shift = n = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _fields(b: bytes) -> list[tuple[int, int, object]]:
    i, out = 0, []
    while i < len(b):
        k, i = _read_varint(b, i)
        fno, wire = k >> 3, k & 7
        if wire == 0:
            v, i = _read_varint(b, i)
            out.append((fno, wire, v))
        elif wire == 2:
            ln, i = _read_varint(b, i)
            out.append((fno, wire, b[i : i + ln]))
            i += ln
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def decode(b64: str) -> TFS:
    raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    legs, adults, seat, trip = [], 0, 1, ONE_WAY
    for fno, _w, v in _fields(raw):
        if fno == 3:
            date = org = dst = ""
            stops = None
            airlines = []
            for f2, _w2, v2 in _fields(v):  # type: ignore[arg-type]
                if f2 == 2:
                    date = v2.decode()  # type: ignore[union-attr]
                elif f2 == 5:
                    stops = v2  # type: ignore[assignment]
                elif f2 == 6:
                    airlines.append(v2.decode())  # type: ignore[union-attr]
                elif f2 in (13, 14):
                    code = next((x.decode() for f3, _w3, x in _fields(v2) if f3 == 2), "")  # type: ignore[union-attr,arg-type]
                    if f2 == 13:
                        org = code
                    else:
                        dst = code
            legs.append(Leg(date, org, dst, stops, tuple(airlines)))
        elif fno == 8 and v == ADULT:
            adults += 1
        elif fno == 9:
            seat = v  # type: ignore[assignment]
        elif fno == 19:
            trip = v  # type: ignore[assignment]
    return TFS(tuple(legs), adults or 1, SEAT_REV.get(seat, Cabin.ECONOMY), trip)  # type: ignore[arg-type]
