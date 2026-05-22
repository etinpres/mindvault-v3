#!/usr/bin/env python3
"""Arctic-Embed-L v2.0 KO (dragonkue) MLX HTTP 임베딩 서버.

POST /embed {"input": str, "kind": "query"|"passage"}  → {"vector": [1024 floats]}
GET  /health                                            → {"ok": true, "model": "arctic-ko-mlx-4bit"}

Sprint 9: BGE-M3(port 8081) A/B 후보. Snowflake Arctic Embed L v2.0의 한국어
fine-tune. CLS pooling + "query: " prefix + L2 normalize 적용 (BGE-M3와 다름).
mlx_embeddings 패키지로 XLM-RoBERTa 4bit 양자화본을 메모리 상주시킨다.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlx.core as mx
from mlx_embeddings.utils import load_model, load_tokenizer

MODEL_DIR = Path.home() / ".cache" / "mlx-arctic-ko"
MODEL_LABEL = "arctic-ko-mlx-4bit"
HOST = "127.0.0.1"
PORT = 8081
EMBED_DIM = 1024
MAX_INPUT_CHARS = 32_000  # 8192 토큰 cap의 안전 마진
QUERY_PREFIX = "query: "  # config_sentence_transformers.json 명시

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("arctic-ko")

log.info("Loading Arctic-ko from %s", MODEL_DIR)
_model = load_model(MODEL_DIR)
if isinstance(_model, tuple):
    _model = _model[0]
_tokenizer = load_tokenizer(MODEL_DIR)
# Metal GPU command queue race 회피 (mlx_embeddings 모델 thread-safe 아님).
_model_lock = threading.Lock()
log.info("Arctic-ko ready on %s:%d", HOST, PORT)


def embed(text: str, kind: str = "passage") -> list[float]:
    """텍스트 → 1024-dim 정규화된 dense 벡터 (CLS pooled + L2 normalized).

    kind="query" 일 때만 "query: " prefix 자동 부착. Arctic Embed L v2.0의 학습
    설정과 일치시켜야 정확도 보장.
    """
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS]
    if kind == "query":
        text = QUERY_PREFIX + text
    with _model_lock:
        tokens = _tokenizer.encode(text)
        input_ids = mx.array([tokens])
        output = _model(input_ids)
        # CLS token pooling (1_Pooling/config.json: pooling_mode_cls_token=true)
        cls = output.last_hidden_state[:, 0, :]
        # L2 normalize (modules.json: 2_Normalize 적용)
        norm = mx.sqrt((cls * cls).sum(axis=-1, keepdims=True))
        normalized = cls / norm
        mx.eval(normalized)
        return normalized[0].tolist()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "model": MODEL_LABEL, "dim": EMBED_DIM})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/embed":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw or b"{}")
            text = body.get("input")
            kind = body.get("kind", "passage")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("input must be a non-empty string")
            if kind not in ("query", "passage"):
                raise ValueError("kind must be 'query' or 'passage'")
            vector = embed(text, kind=kind)
            if len(vector) != EMBED_DIM:
                raise RuntimeError(f"bad embed dim: {len(vector)}")
            self._send_json(200, {"vector": vector})
        except Exception as exc:
            log.exception("embed fail")
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):
        log.info(fmt % args)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("serving on http://%s:%d", HOST, PORT)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
