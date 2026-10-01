"""每进程独立配置文件的覆盖。

背景：Bot 与日常对话共用一份 `.env` 时，两者的 API 用量和账单混在一起。
拆开的做法是让 Bot 用 `QQBOT_ENV_FILE` 指向自己的配置文件。

这里要钉死的核心是**不合并**：两份文件不会互相渗透，否则"我明明改了却没生效"
会变成凭据类配置最难查的一类问题。
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import dev_config


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_default_env_file_when_variable_unset() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(dev_config.ENV_FILE_VARIABLE, None)
        assert dev_config.resolve_env_file() == dev_config.DEFAULT_ENV_FILE


def test_configured_env_file_is_used() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = _write(Path(tmp), ".env.bot", "QQBOT_API_KEY=bot-key\n")
        with patch.dict(os.environ, {dev_config.ENV_FILE_VARIABLE: str(target)}):
            assert dev_config.resolve_env_file() == target


def test_relative_env_file_resolves_against_project_root() -> None:
    with patch.dict(os.environ, {dev_config.ENV_FILE_VARIABLE: ".env.bot.example"}):
        resolved = dev_config.resolve_env_file()
    assert resolved.is_absolute()
    assert resolved.name == ".env.bot.example"


def test_missing_configured_file_falls_back_to_default() -> None:
    """显式指定却不存在时回落到默认，而不是静默什么都不加载。"""

    with patch.dict(os.environ, {dev_config.ENV_FILE_VARIABLE: ".env.does-not-exist"}):
        assert dev_config.resolve_env_file() == dev_config.DEFAULT_ENV_FILE


def test_loading_custom_file_does_not_merge_default() -> None:
    """自定义文件里没有的键，不能从默认 .env 里"补"过来。"""

    with tempfile.TemporaryDirectory() as tmp:
        custom = _write(Path(tmp), ".env.bot", "QQBOT_API_KEY=bot-key\n")
        # 造一个含别的键的"默认文件"，确认它不会被读取。
        default_like = _write(Path(tmp), ".env", "QQBOT_API_MODEL=from-default\n")

        with patch.dict(os.environ, {}, clear=True):
            dev_config.load_env_file(custom)
            assert os.environ.get("QQBOT_API_KEY") == "bot-key"
            assert "QQBOT_API_MODEL" not in os.environ, "不应从默认文件补键"

        assert default_like.exists()  # 只是确认测试构造正确


def test_real_environment_wins_over_file() -> None:
    """真实环境变量优先于配置文件——原有语义不变。"""

    with tempfile.TemporaryDirectory() as tmp:
        custom = _write(Path(tmp), ".env.bot", "QQBOT_API_KEY=from-file\n")
        with patch.dict(os.environ, {"QQBOT_API_KEY": "from-env"}, clear=False):
            dev_config.load_env_file(custom)
            assert os.environ["QQBOT_API_KEY"] == "from-env"


def test_parses_quotes_comments_and_export_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        custom = _write(
            Path(tmp), ".env.bot",
            "# 注释\n"
            "\n"
            "export QQBOT_API_KEY=\"quoted-key\"\n"
            "QQBOT_API_MODEL='single-quoted'\n"
            "QQBOT_TYPING_SIM=1\n"
            "这不是键值对\n",
        )
        parsed = dev_config._parse_env_file(custom)
    assert parsed["QQBOT_API_KEY"] == "quoted-key"
    assert parsed["QQBOT_API_MODEL"] == "single-quoted"
    assert parsed["QQBOT_TYPING_SIM"] == "1"
    assert "这不是键值对" not in parsed


def test_bot_example_template_is_complete_and_secret_free() -> None:
    """模板必须可复制即用，且不能含真实凭据。"""

    template = Path(".env.bot.example")
    assert template.is_file(), "缺少 Bot 独立配置模板"
    values = dev_config._parse_env_file(template)
    # 关键项都在，否则复制过去会退回代码默认值。
    for key in ("QQBOT_API_BASE_URL", "QQBOT_API_MODEL", "QQBOT_API_KEY",
                "QQBOT_MEMORY_ENABLED", "ONEBOT_WS_HOST", "ONEBOT_WS_PORT",
                "QQBOT_SNOWLUMA_HTTP_BASE_URL", "QQBOT_GROUP_LISTEN", "QQBOT_TYPING_SIM"):
        assert key in values, f"模板缺少 {key}"
    # 占位符，不是真 key。
    assert values["QQBOT_API_KEY"].startswith("replace-")


def test_agent_keys_fall_back_to_the_main_key() -> None:
    """判定 / 记忆的 key 不配就回落主 key；配了就用自己的。

    分 key 不隔离并发限额与缓存容量（那是账号级的，KVCache 隔离靠 user_id），
    所以回落必须是安全的默认：只配一把 key 的部署照常能跑。
    """

    with patch.dict(os.environ, {"QQBOT_API_KEY": "main-key"}, clear=False):
        os.environ.pop("QQBOT_JUDGE_API_KEY", None)
        os.environ.pop("QQBOT_MEMORY_API_KEY", None)
        assert dev_config.agent_api_key("QQBOT_JUDGE_API_KEY") == "main-key"
        assert dev_config.agent_api_key("QQBOT_MEMORY_API_KEY") == "main-key"

        os.environ["QQBOT_JUDGE_API_KEY"] = "judge-key"
        assert dev_config.agent_api_key("QQBOT_JUDGE_API_KEY") == "judge-key"
        assert dev_config.agent_api_key("QQBOT_MEMORY_API_KEY") == "main-key"


def test_env_bot_is_gitignored() -> None:
    """真实配置文件绝不能进版本库。"""

    patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.bot.example" in patterns
