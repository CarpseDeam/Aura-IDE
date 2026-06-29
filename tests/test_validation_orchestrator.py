from __future__ import annotations

from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    NO_TESTS_COLLECTED,
    PRODUCT_VALIDATION_FAILED,
    TEST_SELECTION_EMPTY,
    VALIDATION_WRONG_WORKING_DIRECTORY,
    classify_validation_run,
    parse_validation_command,
)


def test_parses_trailing_pytest_outcome_prose() -> None:
    parsed = parse_validation_command(
        "python -m pytest tests/test_worker_summary_card.py "
        "-k worker_summary_card_inserted_by_default -x passes",
        source="explicit_task_command",
    )

    assert parsed.command == (
        "python -m pytest tests/test_worker_summary_card.py "
        "-k worker_summary_card_inserted_by_default -x"
    )
    assert parsed.expected_outcome == "passes"
    assert parsed.normalized is True
    assert parsed.normalization_reason == "trailing outcome prose token"


def test_does_not_blindly_strip_npm_argument() -> None:
    parsed = parse_validation_command("npm test passes")

    assert parsed.command == "npm test passes"
    assert parsed.expected_outcome == ""
    assert parsed.normalized is False


def test_parses_shell_comment_outcome_prose() -> None:
    parsed = parse_validation_command("pytest tests/test_x.py # passes")

    assert parsed.command == "pytest tests/test_x.py"
    assert parsed.expected_outcome == "passes"
    assert parsed.normalization_reason == "trailing comment outcome prose"


def test_normalizes_cd_wrapper_to_cwd() -> None:
    parsed = parse_validation_command("cd companion-web && npm run build")

    assert parsed.command == "npm run build"
    assert parsed.cwd == "companion-web"
    assert parsed.normalized is True
    assert parsed.normalization_reason == "cd wrapper"


def test_normalizes_npm_prefix_to_cwd() -> None:
    parsed = parse_validation_command("npm --prefix companion-web run build")

    assert parsed.command == "npm run build"
    assert parsed.cwd == "companion-web"
    assert parsed.normalized is True


def test_normalizes_npm_c_to_cwd() -> None:
    parsed = parse_validation_command("npm -C companion-web run build")

    assert parsed.command == "npm run build"
    assert parsed.cwd == "companion-web"


def test_normalizes_pnpm_c_to_cwd() -> None:
    parsed = parse_validation_command("pnpm -C companion-web build")

    assert parsed.command == "pnpm build"
    assert parsed.cwd == "companion-web"


def test_normalizes_yarn_cwd_to_cwd() -> None:
    parsed = parse_validation_command("yarn --cwd companion-web build")

    assert parsed.command == "yarn build"
    assert parsed.cwd == "companion-web"


def test_prose_validation_text_is_malformed() -> None:
    parsed = parse_validation_command("Run pytest and make sure it passes")

    assert parsed.malformed is True
    result = classify_validation_run(parsed, exit_code=None, output="", ok=False)
    assert result.classification == MALFORMED_VALIDATION_COMMAND
    assert result.counts_as_product_failure is False


def test_classifies_pytest_missing_prose_token_as_malformed() -> None:
    parsed = parse_validation_command(
        "python -m pytest tests/test_worker_summary_card.py "
        "-k worker_summary_card_inserted_by_default -x passes",
    )
    result = classify_validation_run(
        parsed,
        exit_code=4,
        output="ERROR: file or directory not found: passes\n",
        ok=False,
    )

    assert result.classification == MALFORMED_VALIDATION_COMMAND
    assert result.counts_as_validation is False
    assert result.counts_as_product_failure is False


def test_classifies_real_pytest_assertion_failure_as_product_failure() -> None:
    parsed = parse_validation_command("python -m pytest tests/test_x.py -x")
    result = classify_validation_run(
        parsed,
        exit_code=1,
        output="FAILED tests/test_x.py::test_a - AssertionError: nope",
        ok=False,
    )

    assert result.classification == PRODUCT_VALIDATION_FAILED
    assert result.counts_as_validation is True
    assert result.counts_as_product_failure is True


def test_classifies_collected_zero_tests_as_non_product_issue() -> None:
    parsed = parse_validation_command("pytest tests/test_x.py -k missing")
    result = classify_validation_run(
        parsed,
        exit_code=5,
        output="collected 0 items\n",
        ok=False,
    )

    assert result.classification == NO_TESTS_COLLECTED
    assert result.counts_as_product_failure is False


def test_classifies_empty_selection_as_non_product_issue() -> None:
    parsed = parse_validation_command("pytest tests/test_x.py -k missing")
    result = classify_validation_run(
        parsed,
        exit_code=5,
        output="collected 12 items / 12 deselected / 0 selected\n",
        ok=False,
    )

    assert result.classification == TEST_SELECTION_EMPTY
    assert result.counts_as_product_failure is False


def test_classifies_missing_package_manifest_as_validation_command_issue() -> None:
    parsed = parse_validation_command("npm run build")
    result = classify_validation_run(
        parsed,
        exit_code=254,
        output="npm ERR! enoent ENOENT: no such file or directory, open 'C:\\repo\\package.json'\n",
        ok=False,
    )

    assert result.classification == VALIDATION_WRONG_WORKING_DIRECTORY
    assert result.counts_as_validation is False
    assert result.counts_as_product_failure is False
    assert result.user_action == "fix_validation_command"
