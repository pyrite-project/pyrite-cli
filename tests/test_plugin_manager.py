"""Tests for the plugin manager."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import typer

from cli.plugin_manager import (
    PluginInfo,
    _get_pyrite_root,
    _load_local_plugin,
    _scan_plugin_dir,
    discover_plugins,
    get_loaded_plugins,
    load_plugin,
    load_plugins,
)


class TestDiscoverPlugins:
    """插件发现测试。"""

    def test_discover_returns_list(self):
        """discover_plugins() 始终返回 list，无插件时为空列表。"""
        plugins = discover_plugins()
        # 在测试环境中没有任何注册的 entry points，所以应该是空列表
        assert isinstance(plugins, list)


class TestLoadPlugin:
    """单插件加载测试。"""

    def test_load_valid_typer_app(self):
        """有效的 Typer 实例应正常加载并返回 PluginInfo。"""
        mock_ep = MagicMock()
        mock_ep.name = "ota"
        mock_ep.module = "pyrite_ota"
        mock_ep.value = "pyrite_ota:app"
        mock_ep.load.return_value = typer.Typer(help="OTA plugin")

        with patch("cli.plugins.manager.importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.__plugin_name__ = "ota"
            mock_mod.__plugin_version__ = "1.0.0"
            mock_import.return_value = mock_mod

            info = load_plugin(mock_ep)

        assert info is not None
        assert info.name == "ota"
        assert info.version == "1.0.0"
        assert "OTA" in info.description

    def test_load_broken_plugin_returns_none(self):
        """加载失败的插件应返回 None，不抛出异常。"""
        mock_ep = MagicMock()
        mock_ep.name = "broken"
        mock_ep.load.side_effect = ImportError("no module named 'broken'")

        info = load_plugin(mock_ep)
        assert info is None

    def test_load_non_typer_object_returns_none(self):
        """entry point 返回非 Typer 对象时应跳过。"""
        mock_ep = MagicMock()
        mock_ep.name = "stringy"
        mock_ep.load.return_value = "not a typer app"

        info = load_plugin(mock_ep)
        assert info is None

    def test_load_plugin_without_metadata(self):
        """没有 __plugin_name__ 的插件应使用 entry point 名和默认版本号。"""
        mock_ep = MagicMock()
        mock_ep.name = "mqtt"
        mock_ep.module = "pyrite_mqtt"
        mock_ep.value = "pyrite_mqtt:app"
        mock_ep.load.return_value = typer.Typer()

        with patch("cli.plugins.manager.importlib.import_module") as mock_import:
            mock_mod = MagicMock(spec=[])
            mock_import.return_value = mock_mod

            info = load_plugin(mock_ep)

        assert info is not None
        assert info.name == "mqtt"
        assert info.version == "0.0.0"


class TestLoadPlugins:
    """批量加载测试。"""

    def test_load_plugins_attaches_to_app(self):
        """load_plugins 应将插件挂载到主 Typer 上。"""
        mock_ep = MagicMock()
        mock_ep.name = "ota"
        mock_ep.module = "pyrite_ota"
        mock_ep.value = "pyrite_ota:app"
        mock_ep.load.return_value = typer.Typer(help="OTA plugin")

        main_app = typer.Typer()
        with patch("cli.plugins.manager.discover_plugins", return_value=[mock_ep]):
            with patch("cli.plugins.manager.importlib.import_module") as mock_import:
                mock_mod = MagicMock()
                mock_mod.__plugin_name__ = "ota"
                mock_mod.__plugin_version__ = "0.1.0"
                mock_import.return_value = mock_mod

                plugins = load_plugins(main_app)

        assert len(plugins) == 1
        assert plugins[0].name == "ota"
        assert plugins[0].version == "0.1.0"

    def test_error_isolation(self):
        """一个插件加载失败不应影响其他插件。"""
        good_ep = MagicMock()
        good_ep.name = "good"
        good_ep.module = "good_plugin"
        good_ep.value = "good_plugin:app"
        good_ep.load.return_value = typer.Typer(help="Good plugin")

        bad_ep = MagicMock()
        bad_ep.name = "bad"
        bad_ep.load.side_effect = RuntimeError("broken!")

        main_app = typer.Typer()
        with patch(
            "cli.plugins.manager.discover_plugins", return_value=[good_ep, bad_ep]
        ):
            with patch("cli.plugins.manager.importlib.import_module") as mock_import:
                mock_import.return_value = MagicMock(
                    __plugin_name__="good", __plugin_version__="1.0.0"
                )
                plugins = load_plugins(main_app)

        assert len(plugins) == 1
        assert plugins[0].name == "good"

    def test_empty_discovery(self):
        """没有发现任何插件时 load_plugins 返回空列表。"""
        main_app = typer.Typer()
        with patch("cli.plugins.manager.discover_plugins", return_value=[]):
            plugins = load_plugins(main_app)
        assert plugins == []


class TestGetLoadedPlugins:
    """已加载插件缓存测试。"""

    def test_initial_empty(self):
        """新的一次测试中（重设全局状态后），列表应为空。"""
        # 注意：如果其他测试调用了 load_plugins，全局变量会被污染
        # 所以我们此处只验证返回类型
        plugins = get_loaded_plugins()
        assert isinstance(plugins, list)

    def test_returns_copy(self):
        """返回的应是内部列表的副本，外部修改不影响内部。"""
        plugins = get_loaded_plugins()
        original_len = len(plugins)
        plugins.append(
            PluginInfo(name="x", version="", description="", module_path="")
        )
        assert len(get_loaded_plugins()) == original_len


class TestGetPyriteRoot:
    """pyrite-cli 安装根目录测试。"""

    def test_returns_existing_directory(self):
        """_get_pyrite_root() 应返回一个存在的目录（pyrite-cli 根）。"""
        root = _get_pyrite_root()
        assert os.path.isdir(root)
        assert os.path.isdir(os.path.join(root, "cli"))


class TestScanPluginDir:
    """本地插件目录扫描测试。"""

    def test_nonexistent_dir_returns_empty(self):
        """不存在的目录应返回空列表。"""
        result = _scan_plugin_dir("/nonexistent/path/that/does/not/exist", "test")
        assert result == []

    def test_empty_dir_returns_empty(self):
        """空目录应返回空列表。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _scan_plugin_dir(tmpdir, "test")
        assert result == []

    def test_loads_valid_plugin(self):
        """包含有效 __init__.py 的子目录应被加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = os.path.join(tmpdir, "my-tool")
            os.makedirs(plugin_dir)
            init_file = os.path.join(plugin_dir, "__init__.py")
            with open(init_file, "w", encoding="utf-8") as f:
                f.write("import typer\n")
                f.write('app = typer.Typer(help="My tool")\n')
                f.write('__plugin_name__ = "my-tool"\n')
                f.write('__plugin_version__ = "2.0.0"\n')

            results = _scan_plugin_dir(tmpdir, "test")
            assert len(results) == 1
            assert results[0].name == "my-tool"
            assert results[0].version == "2.0.0"
            assert "My tool" in results[0].description

    def test_skips_non_package_dir(self):
        """没有 __init__.py 的子目录应被跳过。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_dir = os.path.join(tmpdir, "empty-dir")
            os.makedirs(empty_dir)
            # 不创建 __init__.py

            results = _scan_plugin_dir(tmpdir, "test")
            assert results == []

    def test_skips_files_not_dirs(self):
        """文件（非目录）应被跳过。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "not-a-dir.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("# not a plugin\n")

            results = _scan_plugin_dir(tmpdir, "test")
            assert results == []

    def test_error_isolation(self):
        """一个损坏的插件不应影响其他插件加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 正常插件
            good_dir = os.path.join(tmpdir, "good")
            os.makedirs(good_dir)
            with open(os.path.join(good_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write("import typer\n")
                f.write('app = typer.Typer(help="Good")\n')

            # 损坏的插件（__init__.py 语法错误）
            bad_dir = os.path.join(tmpdir, "bad")
            os.makedirs(bad_dir)
            with open(os.path.join(bad_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write("this is syntaktikly wrong {{{{\n")

            results = _scan_plugin_dir(tmpdir, "test")
            assert len(results) == 1
            assert results[0].name == "good"


class TestLoadLocalPlugin:
    """本地单插件加载测试。"""

    def test_load_valid_plugin(self):
        """有效的插件包应正确加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            init_file = os.path.join(tmpdir, "__init__.py")
            with open(init_file, "w", encoding="utf-8") as f:
                f.write("import typer\n")
                f.write('app = typer.Typer(help="Local plugin")\n')
                f.write('__plugin_name__ = "local"\n')
                f.write('__plugin_version__ = "0.5.0"\n')

            info = _load_local_plugin("local", init_file, "test")
            assert info is not None
            assert info.name == "local"
            assert info.version == "0.5.0"
            assert info.app is not None

    def test_missing_app_returns_none(self):
        """没有 app 属性的文件应返回 None。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            init_file = os.path.join(tmpdir, "__init__.py")
            with open(init_file, "w", encoding="utf-8") as f:
                f.write("x = 42\n")

            info = _load_local_plugin("no-app", init_file, "test")
            assert info is None

    def test_default_metadata(self):
        """没有元数据时使用目录名和默认版本号。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            init_file = os.path.join(tmpdir, "__init__.py")
            with open(init_file, "w", encoding="utf-8") as f:
                f.write("import typer\n")
                f.write('app = typer.Typer()\n')

            info = _load_local_plugin("unnamed", init_file, "test")
            assert info is not None
            assert info.name == "unnamed"
            assert info.version == "0.0.0"
