import pytest

from voice.callsign import normalize_callsign, spoken_callsign


@pytest.mark.parametrize(
    "raw,expected",
    [
        # airline telephony -> ICAO
        ("United twelve thirty-four", "UAL1234"),
        ("American one two three", "AAL123"),
        ("Delta four five six", "DAL456"),
        ("Southwest twenty one", "SWA21"),
        ("Alaska three twenty", "ASA320"),
        ("JetBlue six one five", "JBU615"),
        ("Jet Blue six one five", "JBU615"),
        ("Spirit four oh two", "NKS402"),
        ("Frontier Flight one two", "FFT12"),
        ("Frontier twelve", "FFT12"),
        ("SkyWest fifty five eleven", "SKW5511"),
        ("Sky West fifty five eleven", "SKW5511"),
        ("Hawaiian eighty", "HAL80"),
        ("FedEx nine ten", "FDX910"),
        ("Speedbird two eight heavy", "BAW28"),
        ("Speedbird two eight kilo", "BAW28K"),
        ("Allegiant seven seven", "AAY77"),
        # spoken digit forms
        ("United one two three four", "UAL1234"),
        ("United niner niner", "UAL99"),
        ("United nine zero one", "UAL901"),
        ("United nine oh one", "UAL901"),
        ("United twelve oh five", "UAL1205"),
        ("United twelve hundred", "UAL1200"),
        ("United five hundred twelve", "UAL512"),
        ("United double seven", "UAL77"),
        ("United 1234", "UAL1234"),
        ("United 12 34", "UAL1234"),
        ("United flight twelve thirty four", "UAL1234"),
        ("Mayday mayday mayday, United twelve thirty-four", "UAL1234"),
        ("United twelve thirty four with an engine fire", "UAL1234"),
        # N-numbers (NATO letters)
        ("November one two three alpha bravo", "N123AB"),
        ("November four five six seven", "N4567"),
        ("November niner eight seven six five", "N98765"),
        ("Cessna one two three alpha bravo", "N123AB"),
        # already normalized passes through
        ("UAL1234", "UAL1234"),
        ("ual 1234", "UAL1234"),
        ("N123AB", "N123AB"),
        ("BAW28K", "BAW28K"),
        ("UAL twelve thirty four", "UAL1234"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_callsign(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Lufthansa four five six",     # unknown airline
        "United",                       # no number
        "United twelve thirty four fifty six",  # too many digits
        "United zero one two",          # leading zero
        "November alpha bravo",         # N-number needs digits first
        "November one two three four alpha bravo",  # too long
        "November one two three india",  # I/O not used in N-numbers
        "hello there",
    ],
)
def test_unsure_returns_none(raw):
    assert normalize_callsign(raw) is None


@pytest.mark.parametrize(
    "cs,spoken",
    [
        ("UAL1234", "United 12 34"),
        ("SWA512", "Southwest 5 12"),
        ("BAW28K", "Speedbird 28 Kilo"),
        ("N123AB", "November 1 2 3 Alpha Bravo"),
        (None, "unknown callsign"),
    ],
)
def test_spoken(cs, spoken):
    assert spoken_callsign(cs) == spoken
