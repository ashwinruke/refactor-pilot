from refactor_pilot import preproc


def literal(group):
    return preproc.is_literal(group.expr) if group.kind == "if" else None


def flag(name, is_on):
    def decide(group):
        found = preproc.flag_of(group)
        return None if found is None or found[0] != name else (found[1] == is_on)
    return decide


def test_if_zero_block_is_removed():
    out, n = preproc.simplify_conditionals("a;\n#if 0\ndead;\n#endif\nb;\n", literal)
    assert (out, n) == ("a;\nb;\n", 1)


def test_if_zero_with_else_keeps_the_else_branch():
    out, _ = preproc.simplify_conditionals("#if 0\ndead;\n#else\nlive;\n#endif\n", literal)
    assert out == "live;\n"


def test_if_one_keeps_its_body_and_drops_else():
    out, _ = preproc.simplify_conditionals("#if (1)\nlive;\n#else\ndead;\n#endif\n", literal)
    assert out == "live;\n"


def test_nested_groups_inside_a_dead_block_go_with_it():
    source = "#if 0\n#ifdef X\nx;\n#endif\ny;\n#endif\nz;\n"
    assert preproc.simplify_conditionals(source, literal)[0] == "z;\n"


def test_other_conditionals_and_elif_chains_are_left_alone():
    guard = "#ifndef H\n#define H\nint x;\n#endif\n"
    assert preproc.simplify_conditionals(guard, literal) == (guard, 0)
    chain = "#if 0\na;\n#elif defined(Y)\nb;\n#endif\n"
    assert preproc.simplify_conditionals(chain, literal) == (chain, 0)


def test_directive_inside_a_comment_or_string_is_ignored():
    source = '/*\n#if 0\n*/\nconst char* s = "#endif";\n'
    assert preproc.simplify_conditionals(source, literal) == (source, 0)


def test_flag_declared_off():
    source = "#ifdef OLD\nold();\n#else\nnew_();\n#endif\n#ifndef OLD\nalso();\n#endif\n#if defined(OLD)\nx();\n#endif\n"
    out, n = preproc.simplify_conditionals(source, flag("OLD", False))
    assert (out, n) == ("new_();\nalso();\n", 3)


def test_flag_declared_on_and_negated_defined():
    source = "#if !defined(OLD)\na();\n#else\nb();\n#endif\n#ifdef OTHER\nc();\n#endif\n"
    out, n = preproc.simplify_conditionals(source, flag("OLD", True))
    assert (out, n) == ("b();\n#ifdef OTHER\nc();\n#endif\n", 1)


def test_compound_conditions_are_not_guessed():
    source = "#if defined(OLD) && defined(X)\na();\n#endif\n"
    assert preproc.simplify_conditionals(source, flag("OLD", False)) == (source, 0)


def test_fold_true_constant_keeps_then_branch_at_outer_indent():
    source = "int f() {\n    if (kFlag) {\n        return 1;\n    } else {\n        return 2;\n    }\n}\n"
    out, n = preproc.fold_constant_ifs(source, "kFlag", True)
    assert (out, n) == ("int f() {\n    return 1;\n}\n", 1)


def test_fold_false_and_negated():
    source = "void f() {\n    if (!kFlag) {\n        a();\n    } else {\n        b();\n    }\n    if (kFlag) {\n        c();\n    }\n    d();\n}\n"
    out, n = preproc.fold_constant_ifs(source, "kFlag", False)
    assert (out, n) == ("void f() {\n    a();\n\n    d();\n}\n", 2)


def test_fold_leaves_else_if_chains_and_compound_conditions():
    chain = "if (kFlag) {\n    a();\n} else if (x) {\n    b();\n}\n"
    assert preproc.fold_constant_ifs(chain, "kFlag", True) == (chain, 0)
    compound = "if (kFlag && x) {\n    a();\n}\n"
    assert preproc.fold_constant_ifs(compound, "kFlag", True) == (compound, 0)
    other = "if (kFlagOther) {\n    a();\n}\n"
    assert preproc.fold_constant_ifs(other, "kFlag", True) == (other, 0)


def test_fold_handles_nested_braces_and_braces_in_strings():
    source = 'if (kFlag) {\n    if (x) { puts("}"); }\n} else {\n    y();\n}\n'
    out, _ = preproc.fold_constant_ifs(source, "kFlag", True)
    assert out == 'if (x) { puts("}"); }\n'


def test_remove_lines_takes_the_comment_above():
    source = "int a;\n\n// about f\n// more\nstatic void f() {\n}\n\nint b;\n"
    assert preproc.remove_lines(source, 5, 6) == "int a;\n\nint b;\n"


def test_dead_code_diagnostic_must_name_the_symbol_it_is_matched_to():
    from refactor_pilot.recipes.r6_dead_code import _names

    assert _names("Unused function 'legacy_round_cents'", "inventory::legacy_round_cents")
    assert _names("unused function 'ns::helper'", "ns::helper")
    assert not _names("Unused function 'legacy_round_cents'", "inventory::bulk_price")
    assert not _names("unused function 'helper2'", "ns::helper")
