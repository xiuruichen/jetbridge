from hypothesis import given
from hypothesis import strategies as st

from jetbridge.acquire.adapters.gflights_tfs import TFS, Leg, decode
from jetbridge.models.core import Cabin

codes = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=3)


@given(
    org=codes,
    dst=codes,
    day=st.dates().map(lambda d: d.isoformat()),
    adults=st.integers(1, 9),
    cabin=st.sampled_from(list(Cabin)),
    stops=st.one_of(st.none(), st.integers(0, 3)),
)
def test_tfs_roundtrip(org, dst, day, adults, cabin, stops):
    t = TFS((Leg(day, org, dst, stops),), adults=adults, cabin=cabin)
    assert decode(t.b64()) == t


def test_tfs_url_shape():
    t = TFS((Leg("2027-05-28", "CVG", "ICN"),), adults=3, cabin=Cabin.BUSINESS)
    u = t.url()
    assert u.startswith("https://www.google.com/travel/flights/search?tfs=") and "curr=USD" in u
    d = decode(u.split("tfs=")[1].split("&")[0])
    assert d.adults == 3 and d.cabin == Cabin.BUSINESS and d.legs[0].origin == "CVG"
