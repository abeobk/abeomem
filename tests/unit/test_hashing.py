import unicodedata

from abeomem.hashing import MemoFields, content_hash


def _m(**kw):
    return MemoFields(kind=kw.pop("kind", "fix"), title=kw.pop("title", "t"), **kw)


def test_identical_inputs_same_hash():
    a = _m(symptom="s", cause="c", solution="sol")
    b = _m(symptom="s", cause="c", solution="sol")
    assert content_hash(a) == content_hash(b)
    assert len(content_hash(a)) == 32


def test_nfc_vs_nfd_in_topics_same_hash():
    # café in NFC = 4 codepoints; NFD = 5 codepoints (e + combining acute)
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd  # sanity
    a = _m(topics=[nfc, "python"])
    b = _m(topics=[nfd, "python"])
    assert content_hash(a) == content_hash(b)


def test_field_boundary_no_collision():
    # Without \x1f separator, these two would produce identical joined strings.
    a = _m(title="ab", symptom="")
    b = _m(title="a", symptom="b")
    assert content_hash(a) != content_hash(b)


def test_topic_order_does_not_matter():
    a = _m(topics=["python", "asyncio"])
    b = _m(topics=["asyncio", "python"])
    assert content_hash(a) == content_hash(b)


def test_tag_order_does_not_matter():
    a = _m(tags=["flaky", "urgent"])
    b = _m(tags=["urgent", "flaky"])
    assert content_hash(a) == content_hash(b)


def test_different_kinds_different_hash():
    assert content_hash(_m(kind="fix")) != content_hash(_m(kind="gotcha"))


def test_none_vs_empty_treated_the_same():
    a = _m(symptom=None)
    b = _m(symptom="")
    assert content_hash(a) == content_hash(b)
