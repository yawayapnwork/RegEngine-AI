#!/usr/bin/env bash
# Converts a merged HF checkpoint (llm_finetune/merge_adapter.py's output)
# into GGUF and quantizes it for local Ollama inference.
#
# Requires llama.cpp checked out alongside this repo (or set LLAMA_CPP_DIR):
#   git clone https://github.com/ggml-org/llama.cpp ../llama.cpp
#   pip install -r ../llama.cpp/requirements.txt
#
# Usage:
#   ./llm_finetune/export_gguf.sh llm_finetune/merged/sebi-llama3-70b llm_finetune/gguf/sebi-llama3-70b Q4_K_M

set -euo pipefail

MERGED_MODEL_DIR="${1:?Usage: export_gguf.sh <merged-model-dir> <gguf-out-dir> [quant-type]}"
GGUF_OUT_DIR="${2:?Usage: export_gguf.sh <merged-model-dir> <gguf-out-dir> [quant-type]}"
QUANT_TYPE="${3:-Q4_K_M}"  # Q4_K_M: best size/quality tradeoff for CPU+consumer-GPU Ollama inference; use Q8_0 if VRAM allows and format-fidelity on the Rego task matters more than footprint.
LLAMA_CPP_DIR="${LLAMA_CPP_DIR:-../llama.cpp}"

if [ ! -d "$LLAMA_CPP_DIR" ]; then
  echo "llama.cpp not found at $LLAMA_CPP_DIR -- clone it first (see script header) or set LLAMA_CPP_DIR." >&2
  exit 1
fi

mkdir -p "$GGUF_OUT_DIR"

echo "[1/2] Converting HF checkpoint -> f16 GGUF..."
python "$LLAMA_CPP_DIR/convert_hf_to_gguf.py" \
  "$MERGED_MODEL_DIR" \
  --outfile "$GGUF_OUT_DIR/model-f16.gguf" \
  --outtype f16

echo "[2/2] Quantizing f16 GGUF -> $QUANT_TYPE..."
"$LLAMA_CPP_DIR/build/bin/llama-quantize" \
  "$GGUF_OUT_DIR/model-f16.gguf" \
  "$GGUF_OUT_DIR/model-${QUANT_TYPE}.gguf" \
  "$QUANT_TYPE"

echo "Done. GGUF artifact: $GGUF_OUT_DIR/model-${QUANT_TYPE}.gguf"
echo "Next: point llm_finetune/ollama/Modelfile's FROM at this path and run 'ollama create sebi-compliance-llm -f llm_finetune/ollama/Modelfile'."
