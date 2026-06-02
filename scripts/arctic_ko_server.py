#!/usr/bin/env python3
"""Arctic-Embed-L v2.0 KO (dragonkue) MLX HTTP 임베딩 서버.

POST /embed {"input": str, "kind": "query"|"passage"}  → {"vector": [1024 floats]}
GET  /health                                            → {"ok": true, "model": "arctic-ko-mlx-4bit"}

Sprint 9 도입(:8081 임베딩 서버). Snowflake Arctic Embed L v2.0의 한국어
fine-tune. CLS pooling + "query: " prefix + L2 normalize 적용.
mlx_embeddings 패키지로 XLM-RoBERTa 4bit 양자화본을 메모리 상주시킨다.
"""
from __future__ import annotations

import json
import logging
import math
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
MAX_INPUT_CHARS = 32_000  # 토큰화 전 1차 char cap (cheap pre-filter)
# bug-audit 2026-06-02 (#1): 토큰 단위 truncation. XLM-RoBERTa 의
# max_position_embeddings=8194 / model_max_length=8192 를 넘는 토큰열을 forward 에
# 넣으면 크래시가 아니라 position embedding 범위 초과로 all-NaN 벡터가 나온다.
# 이전엔 char cap(32000)만 있어 한국어 장문(~2토큰/자)은 13000~16000 토큰까지
# 통과 → NaN. CLS 가 index 0 이라 앞에서 자르면 CLS 보존 + 유효 벡터 보장.
MAX_TOKENS = 8192
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
        # 토큰 한도 초과 → all-NaN 벡터 방지 (위 MAX_TOKENS 주석 참조). CLS(index 0)
        # 가 보존되도록 앞에서부터 MAX_TOKENS 까지만 사용.
        if len(tokens) > MAX_TOKENS:
            tokens = tokens[:MAX_TOKENS]
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
        # v3.2.6 L1: client 가 hook timeout 으로 socket 끊은 뒤 응답 쓰면
        # BrokenPipeError 가 err.log 에 traceback 누적 (~24건/2061라인). 정상
        # disconnect 이므로 silent log 로 처리해 노이즈 차단.
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError) as e:
            log.warning("client disconnected before response: %s", e)

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
            # bug-audit 2026-06-02 (#1): NaN/Inf 벡터가 200 으로 새 나가는 것을
            # 차단. token truncation 으로 1차 방지하지만, 만일의 비유한 출력은
            # 500 으로 거부해 클라이언트가 defer(재시도)하게 한다. len 가드만으론
            # NaN 을 못 잡는다(길이는 정상).
            if not all(math.isfinite(x) for x in vector):
                raise RuntimeError("non-finite embedding (NaN/Inf)")
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
