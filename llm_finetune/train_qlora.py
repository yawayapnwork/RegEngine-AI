#!/usr/bin/env python3
"""QLoRA (4-bit NF4 + LoRA) domain-adaptation fine-tuning for RegEngine AI.

Adapts an open-source base model (Llama-3-70B-Instruct or Mistral-7B/8x7B
-Instruct) to SEBI securities-law terminology and this project's exact
structured-output conventions (ExtractedComplianceRule JSON, Rego module
shape) using the instruction JSONL produced by
`llm_finetune/dataset/build_dataset.py`.

Why QLoRA specifically: the base models here are large enough (70B) that
full fine-tuning needs multi-node infra RegEngine AI doesn't otherwise
run; NF4 4-bit quantization of the frozen base weights plus a small set of
trainable LoRA adapter weights fits a 70B model on a single 80GB GPU (or a
7B model on a single consumer GPU) while the domain-adaptation task here
-- learning vocabulary, obligation phrasing, and two fixed output formats
-- needs far less capacity change than full fine-tuning would apply
anyway.

Usage:
  python llm_finetune/train_qlora.py \\
      --base-model meta-llama/Meta-Llama-3-70B-Instruct \\
      --train-file llm_finetune/data/train.jsonl \\
      --val-file llm_finetune/data/val.jsonl \\
      --output-dir llm_finetune/checkpoints/sebi-llama3-70b-qlora

  python llm_finetune/train_qlora.py \\
      --base-model mistralai/Mistral-7B-Instruct-v0.3 \\
      --train-file llm_finetune/data/train.jsonl \\
      --val-file llm_finetune/data/val.jsonl \\
      --output-dir llm_finetune/checkpoints/sebi-mistral-7b-qlora \\
      --per-device-batch-size 4 --gradient-accumulation-steps 4
"""
from __future__ import annotations

import argparse
import logging
import os

import torch
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)
from trl import DataCollatorForCompletionOnlyLM, SFTConfig, SFTTrainer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm_finetune.train_qlora")

# Llama-3-Instruct and Mistral-Instruct differ in their chat-template's
# assistant-turn header; DataCollatorForCompletionOnlyLM needs the exact
# literal string to mask the loss so gradients only flow through the
# assistant's response, never the system/user prompt tokens.
_RESPONSE_TEMPLATE_BY_FAMILY = {
    "llama3": "<|start_header_id|>assistant<|end_header_id|>\n\n",
    "mistral": "[/INST]",
}


def _detect_model_family(base_model: str) -> str:
    lowered = base_model.lower()
    if "llama-3" in lowered or "llama3" in lowered:
        return "llama3"
    if "mistral" in lowered or "mixtral" in lowered:
        return "mistral"
    raise ValueError(
        f"Cannot infer chat-template family from base model id {base_model!r}. "
        f"Pass --response-template explicitly for an unlisted model."
    )


def build_quantization_config() -> BitsAndBytesConfig:
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",  # NormalFloat4: matches the roughly-Gaussian distribution of pretrained weights better than plain int4
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,  # quantizes the quantization constants themselves -- ~0.4 bits/param extra saving, negligible accuracy cost
    )


def build_lora_config(base_model: str) -> LoraConfig:
    # Target all linear projections in the attention + MLP blocks, not just
    # q_proj/v_proj: this is a vocabulary/terminology and output-format
    # adaptation task (SEBI-specific nouns, fixed JSON/Rego schemas), which
    # benefits from adapting the MLP's feed-forward representations too --
    # attention-only LoRA under-fits format-following tasks like these.
    target_modules = [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ]
    return LoraConfig(
        r=64,
        lora_alpha=128,
        lora_dropout=0.05,
        target_modules=target_modules,
        bias="none",
        task_type="CAUSAL_LM",
    )


def load_base_model_and_tokenizer(base_model: str):
    quant_config = build_quantization_config()
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=quant_config,
        device_map="auto",
        attn_implementation="flash_attention_2",
        torch_dtype=torch.bfloat16,
    )
    model.config.use_cache = False  # incompatible with gradient checkpointing, enabled below
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"  # required for causal-LM SFT packing
    return model, tokenizer


def formatting_func(example: dict, tokenizer) -> str:
    """SFTTrainer callback: renders one chat-format JSONL record through
    the base model's own chat template, so training-time formatting
    exactly matches what `apply_chat_template` will produce at inference
    time in the vLLM/Ollama deployment (llm_finetune/vllm,
    llm_finetune/ollama) -- a training/serving template mismatch is a
    classic, silent source of degraded fine-tunes."""
    return tokenizer.apply_chat_template(example["messages"], tokenize=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="QLoRA fine-tune an open LLM on SEBI compliance instruction data.")
    parser.add_argument("--base-model", required=True, help="HF hub id, e.g. meta-llama/Meta-Llama-3-70B-Instruct or mistralai/Mistral-7B-Instruct-v0.3")
    parser.add_argument("--train-file", required=True)
    parser.add_argument("--val-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--response-template", default=None, help="Override the auto-detected assistant-turn header string")
    parser.add_argument("--max-seq-length", type=int, default=4096, help="SEBI clauses + full ExtractedComplianceRule JSON can run long; truncation would silently drop verbatim_evidence spans")
    parser.add_argument("--per-device-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--num-train-epochs", type=float, default=3.0)
    parser.add_argument("--warmup-ratio", type=float, default=0.03)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--eval-steps", type=int, default=50)
    parser.add_argument("--save-steps", type=int, default=50)
    args = parser.parse_args()

    family = _detect_model_family(args.base_model)
    response_template = args.response_template or _RESPONSE_TEMPLATE_BY_FAMILY[family]
    logger.info("Model family=%s, response_template=%r", family, response_template)

    model, tokenizer = load_base_model_and_tokenizer(args.base_model)
    lora_config = build_lora_config(args.base_model)
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    train_dataset = load_dataset("json", data_files=args.train_file, split="train")
    val_dataset = load_dataset("json", data_files=args.val_file, split="train")

    collator = DataCollatorForCompletionOnlyLM(response_template=response_template, tokenizer=tokenizer)

    sft_config = SFTConfig(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.per_device_batch_size,
        per_device_eval_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_train_epochs,
        warmup_ratio=args.warmup_ratio,
        logging_steps=args.logging_steps,
        eval_strategy="steps",
        eval_steps=args.eval_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=3,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="paged_adamw_8bit",  # paged optimizer state avoids OOM spikes from the 4-bit base model's activation memory profile
        lr_scheduler_type="cosine",
        max_seq_length=args.max_seq_length,
        packing=False,  # packing would let one training example's assistant tokens attend across a document boundary into the next example -- unsafe for a completion-only loss mask
        report_to=["none"],
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        formatting_func=lambda ex: formatting_func(ex, tokenizer),
        data_collator=collator,
        tokenizer=tokenizer,
    )

    trainer.train()

    # Saves ONLY the LoRA adapter weights (a few hundred MB even for the
    # 70B base) -- see llm_finetune/merge_adapter.py for producing a
    # standalone merged checkpoint for GGUF/Ollama export.
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logger.info("LoRA adapter + tokenizer saved to %s", args.output_dir)


if __name__ == "__main__":
    main()
