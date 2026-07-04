from types import SimpleNamespace

from cli.main import FLASH_NEEDS, flash, flash_program
from cli.reg_commands.debug import BOARD_INFO_NEEDS, DOCTOR_NEEDS, board_info, doctor
from cli.reg_commands.fs import FS_PUT_NEEDS, fs_put
from cli.reg_commands.project import (
    PROJECT_FLASH_NEEDS,
    PROJECT_STATUS_NEEDS,
    project_dev,
    project_flash,
    project_run,
    project_status,
)
from cli.utils.board_features import BoardFeatureStatus, DEFAULT_REGISTRY
from cli.utils.device_context import (
    CommandNeeds,
    DeviceContext,
    command_needs_of,
    needs_with_flash_verify_feature,
    prepare_device,
    resolve_active_tags,
)


class FakeMP:
    def __init__(self):
        self.connected = False
        self.connects = 0
        self.raw_entries = 0
        self.raw_options = []
        self.context_probes = 0
        self.mpy_reads = 0
        self.tag_reads = 0
        self.board_feature_reads = 0
        self.board_feature_ids = []
        self.board_features = (
            BoardFeatureStatus(
                id="ubinascii.crc32",
                category="filesystem",
                status="supported",
                confidence="hasattr-probe",
                macro_hint="MICROPY_PY_BINASCII",
                probe="hasattr(ubinascii, 'crc32')",
            ),
        )
        self.config = SimpleNamespace(
            board_tags={"ESP32": ["ESP32", "wifi"]},
            precheck_mp_version="1.19.1",
            verify="size",
        )
        self.runtime_info = SimpleNamespace(
            implementation="micropython",
            version="1.22.0",
            platform="esp32",
            machine="ESP32 module",
            release="1.22.0",
            sysname="esp32",
            mpy_version=6,
            arch="xtensa",
        )

    @property
    def is_connected(self):
        return self.connected

    def connect(self):
        self.connected = True
        self.connects += 1

    def _enter_raw_repl(self, **kwargs):
        self.raw_entries += 1
        self.raw_options.append(kwargs)

    def ensure_device_context(self):
        self.context_probes += 1
        return DeviceContext.from_runtime_info(self.runtime_info)

    def get_mpy_version(self):
        self.mpy_reads += 1
        return self.runtime_info.mpy_version, self.runtime_info.arch

    def detect_tags(self):
        self.tag_reads += 1
        return {"ESP32"}

    def ensure_board_features(self, feature_ids=None):
        self.board_feature_reads += 1
        self.board_feature_ids.append(feature_ids)
        if feature_ids is None:
            return self.board_features
        wanted = set(feature_ids)
        return tuple(feature for feature in self.board_features if feature.id in wanted)


def test_command_needs_metadata_is_exposed_for_device_context_commands():
    assert command_needs_of(flash) == FLASH_NEEDS
    assert command_needs_of(flash_program) == FLASH_NEEDS
    assert command_needs_of(project_flash) == PROJECT_FLASH_NEEDS
    assert command_needs_of(project_run) == PROJECT_FLASH_NEEDS
    assert command_needs_of(project_status) == PROJECT_STATUS_NEEDS
    assert command_needs_of(project_dev) == PROJECT_FLASH_NEEDS
    assert command_needs_of(fs_put) == FS_PUT_NEEDS
    assert command_needs_of(board_info) == BOARD_INFO_NEEDS
    assert command_needs_of(doctor) == DOCTOR_NEEDS
    assert command_needs_of(flash).repl_preempt is True
    assert command_needs_of(flash_program).repl_preempt is True
    assert command_needs_of(project_flash).repl_preempt is True
    assert command_needs_of(project_run).repl_preempt is True
    assert command_needs_of(project_dev).repl_preempt is True
    assert command_needs_of(fs_put).repl_preempt is True
    assert command_needs_of(project_status).repl_preempt is False
    assert command_needs_of(board_info).repl_preempt is False
    assert command_needs_of(doctor).repl_preempt is False


def test_prepare_device_only_runs_needed_steps_and_returns_context():
    mp = FakeMP()
    prepared = prepare_device(
        mp,
        CommandNeeds(
            connection=True,
            raw_repl=True,
            device_context=True,
            active_tags=True,
            mpy_version=True,
            precheck_version=True,
        ),
    )

    assert mp.connects == 1
    assert mp.raw_entries == 1
    assert mp.raw_options == [{
        "preempt": False,
        "soft_reset_fallback": True,
        "boot_preempt_fallback": True,
    }]
    assert mp.context_probes == 1
    assert mp.tag_reads == 1
    assert mp.mpy_reads == 1
    assert prepared.device_context.version == "1.22.0"
    assert prepared.active_tags == {"ESP32"}
    assert prepared.bytecode_ver == 6
    assert prepared.arch == "xtensa"
    assert prepared.precheck_mp_version == "1.22.0"


def test_prepare_device_passes_raw_repl_strategy_options():
    mp = FakeMP()

    prepare_device(
        mp,
        CommandNeeds(
            connection=True,
            raw_repl=True,
            repl_preempt=True,
            repl_soft_reset_fallback=False,
            repl_boot_preempt_fallback=False,
        ),
    )

    assert mp.raw_options == [{
        "preempt": True,
        "soft_reset_fallback": False,
        "boot_preempt_fallback": False,
    }]


def test_prepare_device_skips_unneeded_context_and_mpy_steps():
    mp = FakeMP()
    prepared = prepare_device(mp, CommandNeeds(connection=True))

    assert mp.connects == 1
    assert mp.raw_entries == 0
    assert mp.context_probes == 0
    assert mp.tag_reads == 0
    assert mp.mpy_reads == 0
    assert mp.board_feature_reads == 0
    assert prepared.device_context is None
    assert prepared.active_tags is None
    assert prepared.bytecode_ver is None


def test_prepare_device_records_optional_cli_feature_fallback_notice():
    mp = FakeMP()
    mp.board_features = (
        BoardFeatureStatus(
            id="ubinascii.crc32",
            category="filesystem",
            status="unsupported",
            confidence="hasattr-probe",
            macro_hint="MICROPY_PY_BINASCII",
            probe="hasattr(ubinascii, 'crc32')",
        ),
    )

    prepared = prepare_device(
        mp,
        CommandNeeds(
            connection=True,
            raw_repl=True,
            device_context=True,
            cli_features=("flash.crc32_verify",),
        ),
    )

    assert mp.board_feature_reads == 1
    assert mp.board_feature_ids == [("ubinascii.crc32",)]
    assert prepared.device_context.feature_status("ubinascii.crc32") == "unsupported"
    assert len(prepared.capability_notices) == 1
    assert prepared.capability_notices[0].fallback == "FallbackToSizeVerify"


def test_prepare_device_errors_for_missing_required_cli_feature():
    feature_id = "test.required_board_feature"
    cli_feature = "test.required_cli_feature"
    try:
        DEFAULT_REGISTRY.register_script_probe(
            feature_id,
            category="test",
            confidence="behaviour-probe",
            macro_hint="TEST_MACRO",
            probe="test probe",
            script=(
                "_pyrite_feature("
                f"{feature_id!r},'test','unsupported','behaviour-probe','TEST_MACRO','test probe'"
                ")"
            ),
            default=False,
        )
        DEFAULT_REGISTRY.register_cli_dependency(cli_feature, feature_id, required=True)
    except ValueError:
        pass

    mp = FakeMP()
    mp.board_features = (
        BoardFeatureStatus(
            id=feature_id,
            category="test",
            status="unsupported",
            confidence="behaviour-probe",
            macro_hint="TEST_MACRO",
            probe="test probe",
        ),
    )

    try:
        prepare_device(
            mp,
            CommandNeeds(
                connection=True,
                raw_repl=True,
                device_context=True,
                cli_features=(cli_feature,),
            ),
        )
    except RuntimeError as exc:
        assert cli_feature in str(exc)
        assert feature_id in str(exc)
    else:
        raise AssertionError("missing required board feature should fail")


def test_needs_with_flash_verify_feature_only_enables_crc32_dependency():
    needs = CommandNeeds(connection=True)

    size_needs = needs_with_flash_verify_feature(needs, SimpleNamespace(verify="size"))
    crc_needs = needs_with_flash_verify_feature(needs, SimpleNamespace(verify="crc32"))

    assert size_needs.cli_features == ()
    assert crc_needs.cli_features == ("flash.crc32_verify",)


def test_resolve_active_tags_manual_target_and_feature_options_do_not_probe():
    mp = FakeMP()

    tags = resolve_active_tags(
        mp,
        target="esp32",
        feature="ble, sensor",
        no_feature="wifi",
    )

    assert tags == {"ESP32", "ble", "sensor"}
    assert mp.tag_reads == 0


def test_resolve_active_tags_auto_detect_applies_feature_options():
    mp = FakeMP()

    tags = resolve_active_tags(mp, feature="ble", no_feature="ESP32")

    assert tags == {"ble"}
    assert mp.tag_reads == 1
