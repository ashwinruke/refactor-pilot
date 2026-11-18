from refactor_pilot.baseline import Snapshot, compare


def snap(warnings=None, tests=None, san=None):
    return Snapshot(warnings=warnings or {}, tests=tests or {}, sanitizer_tests=san)


def test_identical_snapshots_are_ok():
    s = snap({"a.cpp|-Wx|m": 2}, {"t": "pass"}, {"t": "pass"})
    assert compare(s, s).ok


def test_existing_warnings_are_tolerated_but_new_ones_are_not():
    before = snap({"a.cpp|-Wx|m": 2})
    assert compare(before, snap({"a.cpp|-Wx|m": 1})).ok
    reg = compare(before, snap({"a.cpp|-Wx|m": 3, "b.cpp|-Wy|n": 1}))
    assert reg.new_warnings == {"a.cpp|-Wx|m": 1, "b.cpp|-Wy|n": 1}


def test_broken_test_is_a_regression():
    reg = compare(snap(tests={"t": "pass"}), snap(tests={"t": "fail"}))
    assert reg.broken_tests == ["t"] and not reg.ok


def test_test_that_already_failed_is_not_a_regression():
    assert compare(snap(tests={"t": "fail"}), snap(tests={"t": "fail"})).ok


def test_deleting_a_test_is_a_regression():
    reg = compare(snap(tests={"t": "pass", "u": "fail"}), snap(tests={}))
    assert reg.missing_tests == ["t", "u"]


def test_sanitizer_only_failure_is_caught():
    before = snap(tests={"t": "pass"}, san={"t": "pass"})
    after = snap(tests={"t": "pass"}, san={"t": "fail"})
    assert compare(before, after).broken_sanitizer_tests == ["t"]


def test_roundtrip(tmp_path):
    s = snap({"a.cpp|-Wx|m": 1}, {"t": "pass"}, {"t": "pass"})
    s.save(tmp_path / "b.json")
    assert Snapshot.load(tmp_path / "b.json") == s


def test_reworded_warning_is_not_a_new_warning():
    """Changing a type rewrites the text of an existing warning; that is not a regression."""
    before = snap({"w.cpp|-Wsign-compare|comparison of 'int' and 'vector<Item*>::size_type'": 1})
    after = snap({"w.cpp|-Wsign-compare|comparison of 'int' and 'vector<unique_ptr<Item>>::size_type'": 1})
    assert compare(before, after).ok


def test_extra_warning_of_the_same_kind_in_the_same_file_is_caught():
    before = snap({"w.cpp|-Wsign-compare|old text": 1})
    after = snap({"w.cpp|-Wsign-compare|new text": 1, "w.cpp|-Wsign-compare|another": 1})
    reg = compare(before, after)
    assert sum(reg.new_warnings.values()) == 1


def test_same_flag_in_a_different_file_is_new():
    before = snap({"a.cpp|-Wx|m": 1})
    assert compare(before, snap({"b.cpp|-Wx|m": 1})).new_warnings == {"b.cpp|-Wx|m": 1}
