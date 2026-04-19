from abeomem.topics import normalize_topic, normalize_topics


def test_simple_lowercase():
    assert normalize_topic("Python") == "python"


def test_multi_word_to_hyphen():
    assert normalize_topic("memory leak") == "memory-leak"


def test_strip_surrounding_whitespace():
    assert normalize_topic("  memory leak  ") == "memory-leak"


def test_idempotent():
    assert normalize_topic(normalize_topic("Memory Leak")) == normalize_topic("Memory Leak")


def test_dedup_preserves_order():
    assert normalize_topics(["Python", "python", "Go"]) == ["python", "go"]


def test_empty_topic_dropped():
    assert normalize_topics(["", "python"]) == ["python"]


def test_whitespace_only_dropped():
    assert normalize_topics(["   ", "python"]) == ["python"]
