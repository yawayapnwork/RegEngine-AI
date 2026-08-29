#!/usr/bin/env python3
"""Merges a trained LoRA adapter back into the base model's weights,
producing a standalone fp16/bf16 checkpoint.

Two consumers need this merged checkpoint rather than the bare adapter:

  - `llama.cpp`'s `convert_hf_to_gguf.py` (see export_gguf.sh), for Ollama
    local deployment -- GGUF conversion works from a complete model
    directory, not a base model + separate adapter.
  - vLLM CAN serve the adapter unmerged via `--enable-lora` (see
    vllm/docker-compose.vllm.yml) -- prefer that path in production, since
    it lets one base-model server multiplex several LoRA adapters (e.g.
    per-tenant risk-overlay variants) without duplicating the 70B base
    weights on disk per adapter. Use this merge script only for the
    Ollama/GGUF path, or when you specifically want a single fused
    artifact to distribute.

Usage:
  python llm_finetune/merge_adapter.py \\
      --base-model meta-llama/Meta-Llama-3-70B-Instruct \\
      --adapter-dir llm_finetune/checkpoints/sebi-llama3-70b-qlora \\
      --output-dir llm_finetune/merged/sebi-llama3-70b
"""
from __future__ import annotations

import argparse
import logging

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm_finetune.merge_adapter")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge a QLoRA adapter into its base model for GGUF/Ollama export.")
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--adapter-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    logger.info("Loading base model %s in full precision (bf16) for a clean merge -- NOT 4-bit, to avoid re-quantization error compounding onto the adapter delta.", args.base_model)
    base_model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    tokenizer = AutoTokenizer.from_pretrained(args.adapter_dir)

    logger.info("Loading LoRA adapter from %s", args.adapter_dir)
    merged = PeftModel.from_pretrained(base_model, args.adapter_dir)

    logger.info("Merging adapter weights into base model (irreversible fusion of this checkpoint copy only -- the adapter directory itself is untouched)...")
    merged = merged.merge_and_unload()

    merged.save_pretrained(args.output_dir, safe_serialization=True)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("Merged model saved to %s. Next: llm_finetune/export_gguf.sh %s <gguf-out-dir>", args.output_dir, args.output_dir)


if __name__ == "__main__":
    main()
