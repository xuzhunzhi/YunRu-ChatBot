"""本地 ONNX 文本向量：给知识库做语义检索用（CPU 就够，不必等显卡）。

由来（用户 2026-09-28）："现在这个召回太唐了"——词面检索修到极限也就那样：
换个说法问同一件事（"你住在哪" ↔ "你的住处"）命中不了，短问句更是常常一条都召不回。
向量检索解决的就是这一类。

**依赖是可选、且不进仓库运行路径的硬依赖**：`onnxruntime` / `tokenizers` / `numpy`
都只在真正要用的时候延迟导入，缺了就 `is_available() == False`、知识库退回词面检索
（fail-closed，不影响对话）。模型文件放 `data/models/`（`data/` 在 .gitignore 里），
国内从 hf-mirror.com 拉：

    HF_ENDPOINT=https://hf-mirror.com pip install huggingface-hub
    python -c "from huggingface_hub import hf_hub_download as d; \
        d('Xenova/bge-small-zh-v1.5','tokenizer.json',local_dir='data/models/bge-small-zh-v1.5'); \
        d('Xenova/bge-small-zh-v1.5','onnx/model_quantized.onnx',local_dir='data/models/bge-small-zh-v1.5')"

实测（i5 级别 CPU、int8 的 bge-small-zh-v1.5、512 维）：
加载 0.5s，5 条短文本 0.1s（≈20ms/条），734 块语料建向量约 15 秒。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR_NAME = "bge-small-zh-v1.5"
# bge 中文模型的官方用法：**查询**前面加这句指令，文档不加。实测对短问句有帮助。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
MAX_TOKENS = 512
BATCH_SIZE = 32


def default_model_dir() -> Path:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "models" / DEFAULT_MODEL_DIR_NAME


def is_available() -> bool:
    """运行库在不在。**只查 import，不加载模型**——启动路径上不该有几百毫秒的加载。"""

    import importlib.util

    return all(importlib.util.find_spec(name) for name in ("onnxruntime", "tokenizers", "numpy"))


class OnnxEmbedder:
    """一个本地 ONNX 句向量模型。`encode()` 返回 L2 归一化后的 float 列表。

    归一化放在这里，余弦相似度就退化成点积——检索侧那点算术用 Python 也能扛。
    """

    def __init__(self, model_dir: Path | str | None = None, *, threads: int = 0) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_dir = Path(model_dir) if model_dir else default_model_dir()
        onnx_path = self.model_dir / "onnx" / "model_quantized.onnx"
        if not onnx_path.exists():  # 有的导出只给 model.onnx
            onnx_path = self.model_dir / "onnx" / "model.onnx"
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not onnx_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(f"模型文件不齐：{self.model_dir}")

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_padding()
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3  # 只报错，别往日志里刷 warning
        self.session = ort.InferenceSession(
            str(onnx_path), providers=["CPUExecutionProvider"], sess_options=options,
        )
        self._input_names = {item.name for item in self.session.get_inputs()}
        self.model_name = self.model_dir.name
        self.dim = self._probe_dim()

    def _probe_dim(self) -> int:
        return len(self.encode(["云茹"], use_instruction=False)[0])

    def encode(self, texts: list[str], *, use_instruction: bool = False) -> list[list[float]]:
        """把一批文本编码成向量。`use_instruction=True` 时按 bge 的用法给**查询**加指令。"""

        import numpy as np

        if not texts:
            return []
        payload = [QUERY_INSTRUCTION + text if use_instruction else text for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(payload), BATCH_SIZE):
            batch = self.tokenizer.encode_batch(payload[start:start + BATCH_SIZE])
            ids = np.array([item.ids for item in batch], dtype=np.int64)
            mask = np.array([item.attention_mask for item in batch], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]
            mask_f = mask[..., None].astype(np.float32)
            summed = (hidden * mask_f).sum(axis=1)
            counts = np.clip(mask_f.sum(axis=1), 1e-9, None)
            pooled = summed / counts
            norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            vectors.extend((pooled / norms).astype(np.float32).tolist())
        return vectors


def load_embedder(model_dir: Path | str | None = None) -> OnnxEmbedder | None:
    """加载本地模型；**任何一步失败都返回 None**，调用方退回词面检索。"""

    if os.environ.get("QQBOT_EMBED", "1").lower() in {"0", "false", "off"}:
        logger.info("语义检索未启用（QQBOT_EMBED=0）")
        return None
    if not is_available():
        logger.info("语义检索未启用：没装 onnxruntime/tokenizers/numpy")
        return None
    try:
        embedder = OnnxEmbedder(model_dir)
    except Exception:  # noqa: BLE001 - 模型缺失/损坏都只是"这次没有向量"
        logger.warning("语义检索未启用：模型加载失败", exc_info=True)
        return None
    logger.info("语义检索已就绪：%s（%s 维）", embedder.model_dir, embedder.dim)
    return embedder
