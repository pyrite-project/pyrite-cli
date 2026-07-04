import pytest

from cli.utils.board_features import (
    BoardFeatureRegistry,
    BoardFeatureStatus,
    build_probe_script,
    evaluate_cli_feature_requirements,
    parse_board_feature_output,
)


def test_registry_builds_import_probe_script_and_parses_feature_output():
    registry = BoardFeatureRegistry()
    registry.register_import_probe(
        "zlib",
        category="compression",
        macro_hint="MICROPY_PY_DEFLATE",
    )

    script = build_probe_script(registry=registry)

    assert "PYRITE_FEATURES_BEGIN" in script
    assert "import zlib" in script

    parsed = parse_board_feature_output(
        "FEATURE|zlib|compression|supported|import-probe|MICROPY_PY_DEFLATE|import zlib\n"
    )

    assert parsed == (
        BoardFeatureStatus(
            id="zlib",
            category="compression",
            status="supported",
            confidence="import-probe",
            macro_hint="MICROPY_PY_DEFLATE",
            probe="import zlib",
        ),
    )


def test_registry_rejects_duplicate_probe_ids():
    registry = BoardFeatureRegistry()
    registry.register_import_probe("zlib", category="compression", macro_hint="M")

    with pytest.raises(ValueError, match="already registered"):
        registry.register_import_probe("zlib", category="compression", macro_hint="M")


def test_cli_feature_dependency_optional_notice_and_required_error():
    registry = BoardFeatureRegistry()
    registry.register_import_probe("zlib", category="compression", macro_hint="M")
    registry.register_cli_dependency(
        "compress.optional",
        "zlib",
        required=False,
        fallback="FallbackToPlainTransfer",
    )
    registry.register_cli_dependency("compress.required", "zlib", required=True)

    unsupported = (
        BoardFeatureStatus(
            id="zlib",
            category="compression",
            status="unsupported",
            confidence="import-probe",
            macro_hint="M",
            probe="import zlib",
        ),
    )

    notices = evaluate_cli_feature_requirements(
        unsupported,
        ("compress.optional",),
        registry=registry,
    )

    assert len(notices) == 1
    assert notices[0].fallback == "FallbackToPlainTransfer"
    assert "FallbackToPlainTransfer" in notices[0].message

    with pytest.raises(RuntimeError, match="compress.required"):
        evaluate_cli_feature_requirements(
            unsupported,
            ("compress.required",),
            registry=registry,
        )
