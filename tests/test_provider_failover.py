"""钉住 `provider_failover`：主家余额耗尽（HTTP 402）时整条切到兜底那一家。

这一份要钉住的六件事（每一条都对应一种"悄悄出错"的方式）：

1. **primary 模式下它与 `model_config.require` 逐字段相同**——没切的时候，
   运行期解析一个字节都不许变。
2. 只有**主家那一路**报 402 才切；已经在兜底时再报不重复切，别的家报 402 不切
   （否则会来回弹，或者被兜底那一家自己的错误再弹一次）。
3. 兜底的地址**只**来自兜底那条 key 绑的那一家——`QQBOT_<用途>_API_BASE_URL`
   是主家那一路的覆盖，在这里读它就造出"A 家 key 打 B 家地址"
   （2026-10-06 那次三系统 401 的形状）。
4. 没配兜底 key（同家回落链上也没有任何一条有值）时 `resolve_fallback` 是
   `None`、`note_balance_exhausted` 老实返回 False——**fail-closed**，不硬凑端点。
5. 切过去之后，兜底那一家接不住的用途上 `endpoint_for` 老实回 `require`
   ——让它明确失败。这样的用途**只剩 `vision`**（要传图片，mimo 那一路没实测过
   收不收图）；`letter` 自 2026-10-12 起在兜底表里了。
6. `reapply()` 把**已经存在**的 client 四件套 + 闸真的改过去；primary 模式下
   改 0 个、**一条日志都不打**；`restore_primary()` 逐字段（含闸）还原。

> ⚠️ **2026-10-16 的口径回退（如实交代，用户原话："切换回ds。gemini好蠢。"，
> 同日早先那句是"都改ds"）**：**判定回到 deepseek**（`TASKS["judge"]` =
> `{"key": "judge", "model": "deepseek-flash"}`）。2026-10-14～10-16 那段它借
> `reply` 那条 key（xkiro），本文件当时为了留住"deepseek 是这一路的主家"这个例子，
> 把一批用例的驱动从 `judge` 换成了 `letter`；这次**那批 `letter` 驱动的用例保持
> 不动**（`letter` 依旧是 deepseek 那一家上的活证据，换回去只是白改一遍），只把
> "以 **judge 住 xkiro** 为前提"的地方改准：判定那道闸的家改回 deepseek
> （见 `test_the_judge_chain_is_back_on_deepseek`），`reapply` / `restore_primary`
> 两条里 402 的来源从 xkiro 改回 deepseek。
> 不变式（只有主家那一路的 402 才切、兜底地址不来自任务级覆盖、没 key 就
> fail-closed、primary 与 `require` 逐字相同）一个字都没松。

突变说明（把哪一行改坏，这一份就会红）：

- `endpoint_for` 的 primary 分支不再直通 `require` → 第 1 条红；
- `note_balance_exhausted` 去掉"报 402 的必须是主家那一路"那道闸 → 第 2 条红；
- `resolve_fallback` 改成读 `BASE_URL_ENV[task]` → 第 3 条红；
- `resolve_fallback` 在没配兜底 key 时拿主家那把 key 顶替 → 第 4 条红；
- `endpoint_for` 在兜底模式下对 `None` 硬凑一个端点 → 第 5 条红；
- `reapply` 在 primary 模式下也动 client、或乱打日志 → 第 6 条红；
- `restore_primary` 忘了把主家那道闸还回去 → 第 6 条红（`timeout` 那一行）。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

from qq_roleplay_bot import dev_config, model_config, provider_failover
from qq_roleplay_bot.llm_client import OpenAICompatibleClient

#: 本文件会读到的环境变量（跑完原样还回去）。
ALL_VARS: tuple[str, ...] = (
    "QQBOT_API_KEY", "QQBOT_API_BASE_URL", "QQBOT_API_MODEL", "QQBOT_API_PROVIDER",
    "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_MODEL",
    "QQBOT_LETTER_API_KEY", "QQBOT_LETTER_API_BASE_URL", "QQBOT_LETTER_API_MODEL",
    "QQBOT_MAIL_API_KEY", "QQBOT_MAIL_MODEL",
    "QQBOT_JUDGE_API_KEY", "QQBOT_JUDGE_API_BASE_URL", "QQBOT_JUDGE_MODEL",
    "QQBOT_MEMORY_API_KEY", "QQBOT_MEMORY_API_BASE_URL", "QQBOT_MEMORY_MODEL",
    "QQBOT_REVIEW_API_KEY", "QQBOT_REVIEW_API_BASE_URL", "QQBOT_REVIEW_MODEL",
    "QQBOT_VISION_API_KEY", "QQBOT_VISION_API_BASE_URL", "QQBOT_VISION_MODEL",
    # 兜底那条 key（不配就沿它的同家回落链落到那把 mimo 写信 key）。
    "QQBOT_FALLBACK_API_KEY",
)

#: 最常用的一份环境：reply / letter / memory / review / vision 在主家 **deepseek**
#: （reply 与 letter 都借 `judge` 那条 key，其余三路回落到它），兜底那条 key **不配**
#: ——于是它的同家回落链落到 letter 那把实测活着的 mimo key。
#: 2026-10-16 用户「切换回ds」之后 reply 与 judge 都回到借 `judge` 那条 key，
#: 所以这一把就够了（2026-10-14～10-16 那段 reply 走自己那条 key，这份环境里
#: 才需要额外给它一个值）。
MAIN_ENV: dict[str, str] = {
    "QQBOT_JUDGE_API_KEY": "sk-fake-judge",
    "QQBOT_LETTER_API_KEY": "sk-fake-letter",
}


@contextmanager
def _env(**values):
    """把这批变量清空后按 `values` 设好，跑完原样还回去。

    `dev_config.API_KEY`（`.env` 里那把主 key）**一起清掉**：`model_config` 的
    兼容档会看它，不清的话"什么都没配"这一档根本测不出来（本机 `.env` 有值）。
    """

    saved = {name: os.environ.get(name) for name in ALL_VARS}
    saved_key = dev_config.API_KEY
    for name in ALL_VARS:
        os.environ.pop(name, None)
    dev_config.API_KEY = ""
    for name, value in values.items():
        os.environ[name] = value
    try:
        yield
    finally:
        dev_config.API_KEY = saved_key
        for name in ALL_VARS:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


@contextmanager
def _isolated(**values):
    """`_env` + **进程级模式**的进出场。

    兜底模式是**进程单例**：一个测试把它切走，后面所有测试文件都会按兜底那一家
    解析（那是最难查的那种串扰）。所以这里保证：进来先回主家，出去时把**已经存在
    的 client 也一起送回主家**，再清状态。
    """

    provider_failover.reset_for_tests()
    with _env(**values):
        try:
            yield
        finally:
            provider_failover.restore_primary(reason="测试收尾")
            provider_failover.reset_for_tests()


class _Collect(logging.Handler):
    """收日志用（本文件里那条"primary 模式下不许打日志"要断言**一条都没有**）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# --- 1. primary：与 require 逐字段相同 ---------------------------------------


def test_in_primary_mode_every_purpose_is_word_for_word_require() -> None:
    with _isolated(**MAIN_ENV):
        assert provider_failover.mode() == provider_failover.PRIMARY
        for task in model_config.TASK_NAMES:
            got = provider_failover.endpoint_for(task)
            want = model_config.require(task)
            assert got == want, f"{task} 在 primary 模式下与 require 不一致"
            assert got.triple() == want.triple()
            assert got.provider == want.provider
            assert got.key_env == want.key_env
            assert got.own_key == want.own_key


# --- 2. 只有主家那一路的 402 才切 --------------------------------------------


def test_a_402_from_the_main_provider_switches_the_whole_chain() -> None:
    """主家那一路报 402 → 整条切到兜底那一家。

    驱动用的用途是 **`letter`**（住在 deepseek：借 `judge` 那条 key，见 `TASKS`）。
    2026-10-14 那次口径移动把驱动从 `judge` 换成它，2026-10-16 用户「切换回ds」之后
    **保持不动**——`letter` 一直是"deepseek 报 402 → 整条切走"这条不变式的活证据，
    判定那道闸的家另有一条用例钉着（`test_the_judge_chain_is_back_on_deepseek`）。
    """

    with _isolated(**MAIN_ENV):
        assert provider_failover.note_balance_exhausted("deepseek", task="letter") is True
        assert provider_failover.in_fallback() is True
        endpoint = provider_failover.endpoint_for("letter")
        assert endpoint.triple() == (
            "https://api.xiaomimimo.com/v1", "sk-fake-letter", "mimo-v2.6-pro")
        assert endpoint.provider == "mimo"
        # 兜底那一路永远是"借来的那一条"：变量名与 own_key 都要写明这件事
        assert endpoint.key_env == "QQBOT_LETTER_API_KEY"
        assert endpoint.own_key is False
        # 只切了这一次：状态里记的就是本次那个用途与那一家
        assert "letter" in provider_failover.status_line()
        assert "deepseek" in provider_failover.status_line()


def test_the_judge_chain_is_back_on_deepseek() -> None:
    """判定自 2026-10-16 用户「切换回ds」起回到 **deepseek**（用它自己那条 key）——
    余额兜底那道闸认的家跟着回去：**deepseek 报 402 才切**，xkiro 报不算数。

    为什么单列一条、而不是把上一条里的字符串改掉就算：判定的家从 deepseek 换到
    xkiro（2026-10-14）又换回来（2026-10-16）是"语义锚点真的移了"，只改字符串的话，
    "它现在住哪一家、那道闸认谁"就没有活的证据了（上一条的驱动是 `letter`）。
    """

    with _isolated(**MAIN_ENV):
        assert model_config.resolve("judge").provider == "deepseek"
        # 旧家（xkiro 中转）报的不算数——判定的主家已经不是它
        assert provider_failover.note_balance_exhausted("xkiro", task="judge") is False
        assert provider_failover.in_fallback() is False
        # 现在这一家报的才算
        assert provider_failover.note_balance_exhausted("deepseek", task="judge") is True
        assert provider_failover.in_fallback() is True
        endpoint = provider_failover.endpoint_for("judge")
        assert endpoint.triple() == (
            "https://api.xiaomimimo.com/v1", "sk-fake-letter", "mimo-v2.6-pro")
        assert endpoint.key_env == "QQBOT_LETTER_API_KEY"
        assert endpoint.own_key is False
        assert "deepseek" in provider_failover.status_line()
        assert "judge" in provider_failover.status_line()


def test_a_402_from_another_provider_does_not_switch() -> None:
    """不在主家上的那家报 402 不改状态：letter 的主家现在是 deepseek（借 judge 那条，
    2026-10-12 起），所以 mimo 报上来的"余额耗尽"对它不算数；
    没给用途时判不出"这一家是不是主家那一路" → 宁可不切。
    """

    with _isolated(**MAIN_ENV):
        assert provider_failover.note_balance_exhausted("mimo", task="letter") is False
        # 没给用途时判不出"这一家是不是主家那一路" → 宁可不切
        assert provider_failover.note_balance_exhausted("deepseek") is False
        assert provider_failover.in_fallback() is False


def test_a_second_402_does_not_switch_twice() -> None:
    """已经在兜底时再报 402 不改状态（否则就是来回弹）。

    驱动用的用途是 **`letter`**（2026-10-14 起；2026-10-16 用户「切换回ds」之后
    **保持不动**——判定那道闸的家另有一条用例钉着，见
    `test_the_judge_chain_is_back_on_deepseek`）。
    """

    with _isolated(**MAIN_ENV):
        assert provider_failover.note_balance_exhausted("deepseek", task="letter") is True
        # 已经在兜底：再报不许动（否则就是来回弹）
        assert provider_failover.note_balance_exhausted("deepseek", task="letter") is False
        assert provider_failover.note_balance_exhausted("mimo", task="letter") is False
        assert provider_failover.in_fallback() is True


# --- 3. 兜底地址绝不来自任务级覆盖 -------------------------------------------


def test_the_fallback_address_never_comes_from_a_task_level_override() -> None:
    with _isolated(QQBOT_JUDGE_API_BASE_URL="https://somewhere.else.example/v1",
                   **MAIN_ENV):
        # 主家那一趟确实听这个覆盖（第五档的第一档，见 `_base_url_for`）
        assert model_config.resolve("judge").base_url == "https://somewhere.else.example/v1"
        endpoint = model_config.resolve_fallback("judge")
        assert endpoint is not None
        assert endpoint.base_url == "https://api.xiaomimimo.com/v1"
        assert endpoint.api_key == "sk-fake-letter"


# --- 4. 没配兜底 key：fail-closed -------------------------------------------


def test_without_any_fallback_key_it_fails_closed() -> None:
    """没配兜底 key（同家回落链上也没有任何一条有值）→ 老实返回 False，不硬凑端点。

    驱动用的用途是 **`letter`**（2026-10-14 起的驱动，2026-10-16 用户「切换回ds」
    之后保持不动）：`letter` 借 `judge` 那条 key，它有 key、而兜底那条（连它的
    回落链）没有，正是这一条要的形状。
    """

    with _isolated(QQBOT_JUDGE_API_KEY="sk-fake-judge"):
        assert model_config.resolve_fallback("letter") is None
        assert provider_failover.note_balance_exhausted("deepseek", task="letter") is False
        assert provider_failover.in_fallback() is False
        # 没切的时候照旧逐字段等于 require
        assert provider_failover.endpoint_for("letter") == model_config.require("letter")


# --- 5. 兜底那一家接不住的用途：老实失败 -------------------------------------


def test_a_purpose_the_fallback_cannot_serve_fails_honestly() -> None:
    """兜底那一家接不住的用途，切过去之后仍然老实回主家那一路（明确失败，不硬凑）。

    驱动用的用途是 **`letter`**（2026-10-14 起，2026-10-16 用户「切换回ds」之后保持
    不动）：它借 `judge` 那条 key，主家仍在 deepseek 上。
    """

    with _isolated(**MAIN_ENV):
        assert provider_failover.note_balance_exhausted("deepseek", task="letter") is True
        assert provider_failover.in_fallback() is True
        # letter 在 2026-10-12 进了兜底表（主家换成 deepseek 之后），
        # 只剩 vision 不在表里——它要传图片，mimo 那一路没实测过收不收图
        for task in ("vision",):
            assert model_config.resolve_fallback(task) is None, task
            assert (provider_failover.endpoint_for(task).triple()
                    == model_config.require(task).triple()), task


# --- 6. reapply / restore ----------------------------------------------------


def test_reapply_moves_existing_clients_to_the_fallback() -> None:
    """`reapply()` 把已经存在的 client 整条搬到兜底那一家。

    这里的 client 是**判定**那一路，402 的来源是它现在的家 **deepseek**
    （2026-10-16 用户「切换回ds」起它又用自己那条 key；2026-10-14～10-16 那段它借
    `reply` 那条 key、住 xkiro，那时这里写的是 `xkiro`）。
    """

    with _isolated(**MAIN_ENV):
        primary = model_config.resolve("judge")
        client = OpenAICompatibleClient(
            *primary.triple(),
            timeout=5.0, provider=primary.provider, usage_role="judge")
        assert provider_failover.note_balance_exhausted("deepseek", task="judge") is True
        # 默认那条路走的是 `llm_client.clients()` 注册簿（运行期就是这么调的）
        assert provider_failover.reapply() >= 1
        assert (client.base_url, client.api_key, client.model, client.provider) == (
            "https://api.xiaomimimo.com/v1", "sk-fake-letter", "mimo-v2.6-pro", "mimo")
        # 闸也放宽了：主家那 5 秒在 mimo 上（实测中位 36.8s）等于每次都丢
        assert client.timeout == provider_failover.FALLBACK_TIMEOUTS["judge"]


def test_reapply_in_primary_touches_nothing_and_says_nothing() -> None:
    with _isolated(**MAIN_ENV):
        client = OpenAICompatibleClient(
            "https://old.example", "sk-old", "old-model",
            timeout=5.0, provider="deepseek", usage_role="judge")
        handler = _Collect()
        logger = logging.getLogger("qq_roleplay_bot.provider_failover")
        logger.addHandler(handler)
        try:
            assert provider_failover.reapply([client]) == 0
        finally:
            logger.removeHandler(handler)
        assert (client.base_url, client.api_key, client.model, client.provider) == (
            "https://old.example", "sk-old", "old-model", "deepseek")
        assert handler.records == [], [r.getMessage() for r in handler.records]


def test_restore_primary_puts_every_field_back() -> None:
    """`restore_primary()` 逐字段（含闸）把 client 送回**当值主家那一路**。

    ⚠️ 2026-10-16：client 的起始四件套仍取 `model_config.resolve("judge")`（不写死），
    判定现在就住 **deepseek**（用户「切换回ds」之后回到用它自己那条 key；
    2026-10-14～10-16 那段是 xkiro）。
    """

    with _isolated(**MAIN_ENV):
        primary = model_config.resolve("judge")
        client = OpenAICompatibleClient(
            *primary.triple(),
            timeout=5.0, provider=primary.provider, usage_role="judge")
        before = (client.base_url, client.api_key, client.model, client.provider, client.timeout)
        assert provider_failover.note_balance_exhausted("deepseek", task="judge") is True
        assert provider_failover.reapply([client]) == 1
        assert client.timeout != before[-1]  # 兜底那一档确实更宽
        assert provider_failover.restore_primary(reason="测试") is True
        assert (client.base_url, client.api_key, client.model, client.provider,
                client.timeout) == before
        assert provider_failover.in_fallback() is False
        # 已经回主家之后再调一次：不重复做事
        assert provider_failover.restore_primary() is False
