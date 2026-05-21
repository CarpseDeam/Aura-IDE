import aura


def test_import_aura():
    assert aura.__version__ is not None


def test_sanity():
    assert 1 + 1 == 2
