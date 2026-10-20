from catsyncd.inventory import _ignored, _pattern_matches


def test_compiled_ignore_matches_individual_patterns():
    patterns = ("*.pyc", " cache ", "/foo/*/bar/", "*/terminal/*", "a[!x]", "a[", "")
    paths = ("src/foo.pyc", "src/cache/file", "foo/one/bar", "root/foo/one/bar",
             "x/terminal/a", "x/terminal/a/b", "ab", "ax", "a[", "keep.py",
             "src/keep.py", "folder/.git/config", ".catsync/state", "line\nbreak.pyc")
    for rel in paths:
        parts = rel.split("/")
        expected = (".git" in parts or ".catsync" in parts or
                    any(_pattern_matches(rel, parts, p) for p in patterns))
        assert _ignored(rel, patterns) == expected, rel


def test_compiled_ignore_cache_uses_current_configuration():
    patterns = ["*.tmp"]
    assert _ignored("nested/file.tmp", patterns)
    patterns[:] = ["*.log"]
    assert not _ignored("nested/file.tmp", patterns)
    assert _ignored("nested/file.log", iter(patterns))
    assert not _ignored("nested/file.log", ())
