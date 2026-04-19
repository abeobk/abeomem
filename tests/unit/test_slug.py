from abeomem.slug import slugify


def test_spec_example():
    assert slugify("TS build fails after pnpm update") == "ts-build-fails-after-pnpm-update"


def test_accented_chars_stripped():
    assert slugify("café") == "cafe"


def test_empty_returns_untitled():
    assert slugify("") == "untitled"


def test_only_punct_returns_untitled():
    assert slugify("!!!") == "untitled"


def test_length_cap_no_trailing_hyphen():
    long = "a-" * 100
    result = slugify(long)
    assert len(result) <= 60
    assert not result.endswith("-")


def test_spaces_collapse_to_single_hyphen():
    # Underscores are dropped in step 1 (not in [a-z0-9\s-]); only whitespace
    # and hyphens survive to the collapse step per spec §1.6.
    assert slugify("foo  bar") == "foo-bar"
    assert slugify("foo_bar") == "foobar"


def test_multiple_hyphens_collapsed():
    assert slugify("foo---bar") == "foo-bar"


def test_preserves_digits():
    assert slugify("issue 42 fix") == "issue-42-fix"
