# LLM Fine-Tuning Pipeline & Scaffolding (`llm_finetune/`)

## 1. Overview & Current Status

> [!IMPORTANT]
> **A cost-tiered fine-tuning pipeline (QLoRA) is implemented for a self-hosted low-cost model tier; production fine-tuning on a real annotated SEBI corpus is a roadmap item, not yet complete.**
>
> The repository currently contains the **QLoRA training and serving scaffolding** alongside synthetic test fixture generators. No verified production model trained on a real annotated SEBI regulatory corpus is deployed, and no unbenchmarked model accuracy improvements are claimed.

The active Core MVP pipeline (`app/agents/`) executes deterministic extraction and auditing using dual-agent architectures with frontier LLMs (e.g., Qwen2.5-72B-Instruct via Hugging Face Inference) or local offline inference (`LLM_PROVIDER=offline`).

---

## 2. Architecture of the Scaffolding

This directory provides the end-to-end toolchain to train, quantize, and serve self-hosted domain-adapted models once an annotated SEBI regulatory corpus is compiled:

| Component | Path | Description & Role |
|---|---|---|
| **Synthetic Fixture Generator** | `dataset/sample_artifacts.py` | Generates synthetic JSONL fixtures strictly for pipeline smoke-testing. *These fixtures do not constitute regulatory training data.* |
| **Dataset Formatter** | `dataset/format_instructions.py` | Converts real pipeline artifacts (`AuditedComplianceRule`, `ClauseChunk`, `CompiledRego`) into chat-format instruction-tuning records. |
| **Dataset Builder CLI** | `dataset/build_dataset.py` | Filters trustworthy audited rules (`_only_trustworthy`) and splits data into `train.jsonl`, `val.jsonl`, and `test.jsonl`. |
| **QLoRA Trainer** | `train_qlora.py` | Parameter-efficient fine-tuning (4-bit NF4 quantization + LoRA adapters via `trl.SFTTrainer` and `peft`). |
| **LoRA Merger** | `merge_adapter.py` | Merges trained adapter weights back into base model checkpoints for standalone export. |
| **GGUF Exporter** | `export_gguf.sh` | Shell script wrapping `llama.cpp`'s `convert_hf_to_gguf.py` and quantizer for CPU/edge inference. |
| **Ollama Template** | `ollama/Modelfile` | Scaffolding for local deployment and testing under Ollama. |
| **vLLM Serving** | `vllm/docker-compose.vllm.yml` | Multi-tenant dynamic LoRA adapter serving container specification. |
| **Client Test Harness** | `vllm/client_example.py` | Example client validating structured JSON generation from local inference endpoints. |

---

## 3. Training Data Disciplines & Safeguards

1. **Synthetic Fixtures are Non-Regulatory**:
   - `llm_finetune/dataset/sample_artifacts.py` contains small synthetic clause templates solely for exercising scripts without active GPU/database resources.
   - They must never be treated as legal or regulatory training material.
2. **Audit Filtering Gate**:
   - The training set compiler enforces `_only_trustworthy`: extractions with `AuditVerdict.REJECTED` or low fidelity scores are filtered out to prevent reinforcement of hallucinations.
3. **Pre-Flight Regression Guard**:
   - `evals/regression_guard.py` enforces that model checkpoints cannot be promoted without passing baseline extraction and hallucination test suites.

---

## 4. Roadmap & Future Work

The following items are planned for future milestones:

- [ ] **Curated Annotation of SEBI Master Circular Corpus**: Assemble and gold-standard-annotate historical and active SEBI circulars spanning broking, mutual funds, clearing corporations, and depositories.
- [ ] **Domain Adaptation Execution**: Run QLoRA training runs on Llama-3-70B / Mistral base models against the verified corpus.
- [ ] **Empirical Benchmark & Evaluation**: Systematically measure extraction F1, entity resolution accuracy, and OPA compile parity against frontier baselines before enabling the self-hosted low-cost model tier in production.
