#!/usr/bin/env python3
"""对比基线与优化后内容安全预测；要求 JSONL: id, y_true(0/1), score(0..1)
正类 1=违规；每条 id 唯一。两份文件须同样本、同真实标签。
"""
import argparse
import json
from pathlib import Path


def load(path):
    rows = {}
    for i, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        o = json.loads(line)
        key = str(o['id'])
        if key in rows:
            raise ValueError(f'{path}: duplicated id={key}')
        y = int(o['y_true'])
        score = float(o['score'])
        if y not in (0,1) or not 0 <= score <= 1:
            raise ValueError(f'{path}:{i}: y_true should be 0/1 and score in [0,1]')
        rows[key] = (y,score)
    return rows


def metrics(rows, threshold):
    tp = fp = tn = fn = 0
    for true, score in rows.values():
        pred = int(score >= threshold)
        if true == 1 and pred == 1: tp += 1
        elif true == 1: fn += 1
        elif pred == 1: fp += 1
        else: tn += 1
    return {
        'TP':tp,'FN':fn,'FP':fp,'TN':tn,
        'recall': tp / (tp + fn) if tp+fn else None,
        'fpr': fp / (fp + tn) if fp+tn else None,
        'precision': tp / (tp + fp) if tp+fp else None,
        'accuracy': (tp+tn)/max(1,len(rows)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline',required=True)
    ap.add_argument('--candidate',required=True)
    ap.add_argument('--threshold',type=float,default=0.5)
    ap.add_argument('--out',default='paired_report.json')
    args = ap.parse_args()
    if not 0 <= args.threshold <= 1:
        raise ValueError('threshold must be in [0,1]')
    b, c = load(args.baseline), load(args.candidate)
    if not b or set(b) != set(c):
        raise ValueError('两份文件 id 集合不相同，无法做配对对比')
    newly_missed = []
    newly_false_alarm = []
    fixed_miss = []
    fixed_false_alarm = []
    score_diffs = []
    for key in b:
        y, s0 = b[key]
        y1,s1=c[key]
        if y != y1:
            raise ValueError(f'id={key}: y_true mismatch')
        p0,p1 = int(s0 >= args.threshold),int(s1 >= args.threshold)
        score_diffs.append(abs(s1-s0))
        if y==1 and p0==1 and p1==0: newly_missed.append(key)
        if y==0 and p0==0 and p1==1: newly_false_alarm.append(key)
        if y==1 and p0==0 and p1==1: fixed_miss.append(key)
        if y==0 and p0==1 and p1==0: fixed_false_alarm.append(key)
    m0,m1=metrics(b,args.threshold),metrics(c,args.threshold)
    report={
        'threshold':args.threshold,'n':len(b),
        'baseline':m0,'candidate':m1,
        'recall_delta_pp':100*(m1['recall']-m0['recall']) if m0['recall'] is not None and m1['recall'] is not None else None,
        'fpr_delta_pp':100*(m1['fpr']-m0['fpr']) if m0['fpr'] is not None and m1['fpr'] is not None else None,
        'newly_missed_ids':newly_missed,
        'newly_false_alarm_ids':newly_false_alarm,
        'fixed_missed_ids':fixed_miss,
        'fixed_false_alarm_ids':fixed_false_alarm,
        'max_abs_score_delta':max(score_diffs),
        'mean_abs_score_delta':sum(score_diffs)/len(score_diffs)
    }
    Path(args.out).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if not k.endswith('_ids')},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
