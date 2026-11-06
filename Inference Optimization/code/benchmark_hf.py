#!/usr/bin/env python3
"""通用 Hugging Face CausalLM 推理基准；仅适用于标准 generate 接口模型。
运行示例: python code/benchmark_hf.py --model /path/to/model --data samples.jsonl --batch-size 8
JSONL 每行必须含 text 字段。严格禁止静默截断长文本；测试集较大时按用户预先采样。
"""
import argparse
import json
import statistics
import time
from pathlib import Path


def percentile(values, p):
    a = sorted(values)
    if not a:
        return float('nan')
    pos = (len(a) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(a) - 1)
    return a[lo] + (a[hi] - a[lo]) * (pos - lo)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--data', required=True, help='JSONL: each row {"text": "..."}')
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--max-samples', type=int, default=200)
    parser.add_argument('--max-input-tokens', type=int, default=8192)
    parser.add_argument('--max-new-tokens', type=int, default=8)
    parser.add_argument('--warmup-batches', type=int, default=2)
    parser.add_argument('--dtype', choices=['bf16', 'fp16', 'fp32'], default='bf16')
    parser.add_argument('--out', default='benchmark_hf_result.json')
    args = parser.parse_args()
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM

    if not torch.cuda.is_available():
        raise RuntimeError('该脚本需要 NVIDIA CUDA GPU')
    if args.batch_size < 1 or args.max_samples < 1:
        raise ValueError('batch-size/max-samples must be positive')
    dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16, 'fp32': torch.float32}[args.dtype]
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=False)
    if tokenizer.pad_token is None:
        if tokenizer.eos_token is None:
            raise ValueError('tokenizer has neither pad_token nor eos_token')
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = 'left'
    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=dtype, trust_remote_code=False
    ).to('cuda').eval()

    texts = []
    for line in Path(args.data).read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if not isinstance(obj.get('text'), str):
            raise ValueError('每行 JSONL 必须包含字符串 text')
        texts.append(obj['text'])
        if len(texts) >= args.max_samples:
            break
    if not texts:
        raise ValueError('输入文件无有效记录')
    # 原始文本应经过与生产完全一致的 chat template 和标签约束构造。
    # 这里直接使用 text，不对模型任务定义作任何猜测。
    batches = [texts[i:i+args.batch_size] for i in range(0, len(texts), args.batch_size)]

    def once(batch):
        start_total = time.perf_counter()
        tokens = tokenizer(batch, return_tensors='pt', padding=True, truncation=False)
        lengths = tokens['attention_mask'].sum(dim=1).tolist()
        if max(lengths) > args.max_input_tokens:
            raise ValueError(f'存在超长输入 {max(lengths)} Token；已中止，而不是截断')
        t_tokenized = time.perf_counter()
        tokens = {k: v.to('cuda') for k,v in tokens.items()}
        torch.cuda.synchronize()
        t_gpu_start = time.perf_counter()
        with torch.inference_mode():
            output = model.generate(
                **tokens, max_new_tokens=args.max_new_tokens,
                do_sample=False, use_cache=True,
                pad_token_id=tokenizer.pad_token_id,
            )
        torch.cuda.synchronize()
        t_gpu_end = time.perf_counter()
        # generate 有可能提前 EOS；这里只统计实际输出 Tensor 的位置数量，
        # 不能代表所有样本的有效输出 Token 数。
        padded_output_positions = len(batch) * max(0, output.shape[1] - tokens['input_ids'].shape[1])
        return {
            'batch_size': len(batch),
            'raw_input_tokens': int(sum(lengths)),
            'padded_input_tokens': int(tokens['input_ids'].numel()),
            'output_positions': int(padded_output_positions),
            'tokenize_sec': t_tokenized - start_total,
            'gpu_sec': t_gpu_end - t_gpu_start,
            'total_sec': t_gpu_end - start_total,
        }

    for batch in batches[:args.warmup_batches]:
        once(batch)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    results = []
    t0 = time.perf_counter()
    for batch in batches:
        results.append(once(batch))
    torch.cuda.synchronize()
    wall = time.perf_counter() - t0
    n = sum(x['batch_size'] for x in results)
    output = {
        'model': args.model, 'dtype': args.dtype, 'batch_size': args.batch_size,
        'num_samples': n, 'max_new_tokens': args.max_new_tokens,
        'wall_time_sec': wall, 'requests_per_sec': n / wall,
        'input_tokens_per_sec': sum(x['raw_input_tokens'] for x in results) / wall,
        'batch_gpu_latency_ms_mean': 1000 * statistics.mean(x['gpu_sec'] for x in results),
        'batch_gpu_latency_ms_p50': 1000 * percentile([x['gpu_sec'] for x in results], 50),
        'batch_gpu_latency_ms_p95': 1000 * percentile([x['gpu_sec'] for x in results], 95),
        'tokenize_total_sec': sum(x['tokenize_sec'] for x in results),
        'peak_allocated_gib': torch.cuda.max_memory_allocated() / 1024**3,
        'peak_reserved_gib': torch.cuda.max_memory_reserved() / 1024**3,
        'total_unpadded_input_tokens': sum(x['raw_input_tokens'] for x in results),
        'total_padded_input_tokens': sum(x['padded_input_tokens'] for x in results),
        'note': 'P50/P95 是批次 GPU 执行耗时，不是线上单请求 P50/P95；生成输出未解析业务标签。'
    }
    Path(args.out).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(output, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
