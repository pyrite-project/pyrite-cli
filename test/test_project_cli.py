from typer.testing import CliRunner

from cli.main import app


runner = CliRunner()


def test_project_new_help_distinguishes_platform_and_port():
    result = runner.invoke(app, ["project", "new", "--help"])

    assert result.exit_code == 0
    assert "--platform" in result.stdout
    assert "直接指定 MicroPython 平台" in result.stdout
    assert "--port" in result.stdout
    assert "串口号" in result.stdout


def test_project_new_rejects_platform_and_port_together():
    result = runner.invoke(
        app,
        [
            "project",
            "new",
            "demo",
            "--platform",
            "esp32",
            "--port",
            "COM3",
        ],
    )

    assert result.exit_code == 2
    assert "--platform 和 --port 不能同时使用" in result.stderr
