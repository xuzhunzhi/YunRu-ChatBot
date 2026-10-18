"""供应商目录：面板"切换供应商"用的那张表。

由来（2026-10-01 用户）："可以切换供应商"。切换的实质是**换 base_url（可能连 key 一起换）**，
因为所有 agent 都走 OpenAI 兼容的 `/chat/completions`（`llm_client._complete_sync` 里
写死的那条路）。所以这里只需要一张"名字 → 地址"的表，不需要为每个供应商写适配器。

纪律：

1. **别名不许猜**：面板传一个不认识的 provider 名 → 明确报错，不"就近找相似的"。
   凭据类配置最怕的就是"我明明改了却没生效"。
2. **`custom` 表示自己填地址**：那时 `api_base_url` 由面板直接给，这里不参与。
3. 表里只放地址（公开信息），**不放任何 key**。
"""

from __future__ import annotations

#: 供应商 → 默认 base_url。键就是面板下拉框里的值。
#:
#: 2026-10-06：加上 `mimo`（小米 MiMo）。它本来就在 `model_config.PROVIDERS`
#: 里（回复 / 写信那两路绑的是它），面板下拉框少一个选项就会让"换供应商"报错——
#: 那两个表必须能对上，否则用户看得见的选项与实际能用的那家会分叉。
PROVIDERS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "mimo": "https://api.xiaomimimo.com/v1",
    # 2026-10-14（用户给的地址）：OpenAI 兼容中转，里面有 Gemini Flash。回复那一路
    # 现在挂的就是它（`model_config.TASKS["reply"]`）。两张表必须能对上——
    # 面板下拉框少一个选项，"换供应商"就会报错。
    "xkiro": "https://api.xkiro.com/v1",
    "openai": "https://api.openai.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "custom": "",
}

#: 给面板看的中文标签。
LABELS: dict[str, str] = {
    "deepseek": "DeepSeek（官方）",
    "mimo": "小米 MiMo",
    "xkiro": "xkiro 中转（Gemini Flash）",
    "openai": "OpenAI",
    "moonshot": "月之暗面 Kimi",
    "dashscope": "阿里云百炼（通义）",
    "siliconflow": "硅基流动",
    "custom": "自定义地址",
}

#: 只改地址、**不动 key** 的供应商（换地址但凭据可能通用时用得上）。
#: 目前没有这种特例，留一份空名单是为了让"到底动不动 key"这件事有明确的落点。
KEEP_KEY = frozenset()


class UnknownProvider(ValueError):
    """面板给了不认识的供应商名。"""


def _merged_urls() -> dict[str, str]:
    """内置表 + `data/model_providers.json` 里那几家（**文件里的新家也要能选**）。

    为什么要在函数里合并、而且用延迟 import：`provider_registry` 是
    `tests/check_module_removal.py` 列的"可插能力"之一——模块级 import `model_config`
    会让"删掉它核心就起不来"（反向不受限，`runtime.py` 里就是这么用的）。
    文件里同名的条目**优先**（与 `model_config._provider_spec` 同一口径），
    这样面板下拉框里那个地址就是这一趟真正会打过去的地址。
    """

    merged = dict(PROVIDERS)
    try:
        from .model_config import file_provider_specs

        for name, spec in file_provider_specs().items():
            merged[name] = str(spec.get("base_url", "") or "")
    except Exception:  # noqa: BLE001 - 下拉框不该因为一份坏文件而打不开
        return merged
    return merged


def base_url_of(name: str) -> str:
    """取供应商的默认地址。`custom` 返回空串（由调用方用面板填的地址）。"""

    urls = _merged_urls()
    key = str(name or "").strip().casefold()
    if key not in urls:
        raise UnknownProvider(f"不认识的供应商：{name}")
    return urls[key]


def known(name: str) -> bool:
    return str(name or "").strip().casefold() in _merged_urls()


def options() -> list[dict[str, str]]:
    """给面板的下拉框数据（内置那几家在前，文件里新加的家排在后面）。"""

    urls = _merged_urls()
    ordered = list(PROVIDERS) + [name for name in urls if name not in PROVIDERS]
    return [{"value": key, "label": LABELS.get(key, key), "base_url": urls[key]}
            for key in ordered]
