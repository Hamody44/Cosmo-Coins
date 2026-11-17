#!/usr/bin/env python3
"""Bounded exact-five-digit Type1 experiments using training-only template banks."""
from __future__ import annotations
import csv, json, random, time, sys
from pathlib import Path
import numpy as np
from PIL import Image
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from type1 import hamming_distance, normalize_glyph, build_template_bank

ROOT=Path(__file__).resolve().parents[2]; DATA=ROOT/'samples'; POSITIONS=range(5)
BASE=(224,160,192,160,224); THRESHOLDS=(160,176,192,208,224); SHIFTS=(-4,-2,0,2,4)

def rows():
 with (DATA/'label.csv').open(encoding='utf-8-sig',newline='') as f:return [r for r in csv.DictReader(f) if r.get('type','').lower()=='type1']
def images(rs):return {r['filename']:np.asarray(Image.open(DATA/r['filename']).convert('L'),dtype=np.uint8) for r in rs}
def split(rs,seed=41):
 x=list(rs);random.Random(seed).shuffle(x);n=round(len(x)*.2);return x[n:],x[:n]
def bounds(width,margin=.1):
 l,r=round(width*margin),round(width*(1-margin));return [l+(r-l)*i//5 for i in range(6)]
def crop(gray,p,t,margin=.1,shift=0):
 b=bounds(gray.shape[1],margin);return (gray[:,b[p]+shift:b[p+1]+shift]<=t).copy()
def make_bank(rs,ims,thr,margin=.1,shift=0):
 gs=[[crop(ims[r['filename']],p,thr[p],margin,shift) for p in POSITIONS] for r in rs]
 return build_template_bank(gs,[r['answer'] for r in rs])
def rank(g, values):
 ng=normalize_glyph(g);return sorted((min(hamming_distance(ng,t) for t in vs),d) for d,vs in values.items() if vs)
def beam(cands, width=10):
 beams=[('',0.0,[])]
 for pos in POSITIONS:
  nxt=[]
  for text,score,meta in beams:
   for dist,d in cands[pos]:nxt.append((text+d,score+dist,meta+[(pos,d,dist)]))
  beams=sorted(nxt,key=lambda x:(x[1],x[0]))[:width]
 return beams

def classify(gray, banks, configs, *, beam_width=1, retry_margin=None, candidate_limit=3):
 # Merge candidates from fixed base and alternate paths, retaining minimum score per digit.
 allc=[]; margins=[]
 for p in POSITIONS:
  per={}
  for bank,(thr,shift) in zip(banks,configs):
   r=rank(crop(gray,p,thr[p],.1,shift),bank[p])
   for dist,d in r[:candidate_limit]: per[d]=min(per.get(d,999),dist)
  ranked=sorted((v,k) for k,v in per.items()); allc.append(ranked)
  margins.append(ranked[1][0]-ranked[0][0] if len(ranked)>1 else 0)
 primary=beam(allc,beam_width)[0]
 if retry_margin is not None and any(m<retry_margin for m in margins):
  return beam(allc,max(beam_width,10))[0],margins
 return primary,margins

def evaluate(rs,ims,banks,configs,beam_width=1,retry_margin=None,limit=3,errors=False):
 pos=[0]*5;exact=0; low02=low05=0;err=[]
 for r in rs:
  (pred,score,_),ms=classify(ims[r['filename']],banks,configs,beam_width=beam_width,retry_margin=retry_margin,candidate_limit=limit);ans=r['answer'];exact+=pred==ans
  for p,(a,b) in enumerate(zip(pred,ans)):pos[p]+=a==b
  low02+=sum(x<.02 for x in ms);low05+=sum(x<.05 for x in ms)
  if pred!=ans and errors:err.append({'filename':r['filename'],'answer':ans,'predicted':pred,'wrong_positions':[p for p,(a,b) in enumerate(zip(pred,ans)) if a!=b],'score':score,'margins':ms})
 n=len(rs);return {'exact':exact/n,'digit':sum(pos)/(n*5),'position_accuracy':[x/n for x in pos],'position_error':[1-x/n for x in pos],'low_margin_002':low02,'low_margin_005':low05,'errors':err}

def main():
 start=time.time();rs=rows();ims=images(rs);train,val=split(rs)
 # Build bounded threshold and crop alternatives.
 configs=[(BASE,0)]+[(tuple(list(BASE[:3])+[t]+list(BASE[4:])),0) for t in THRESHOLDS]
 configs += [(BASE,s) for s in SHIFTS if s]
 banks=[make_bank(train,ims,t,.1,s) for t,s in configs]
 results=[]
 def run(name,idxs,beam_width=1,retry=None,limit=3):
  bs=[banks[i] for i in idxs];cs=[configs[i] for i in idxs];v=evaluate(val,ims,bs,cs,beam_width,retry,limit,True);results.append({'name':name,'configs':[{'thresholds':list(cs[i][0]),'shift':cs[i][1]} for i in range(len(cs))],'beam_width':beam_width,'retry_margin':retry,'candidate_limit':limit,'validation':v});print(name,round(v['digit'],6),round(v['exact'],6),[round(x,6) for x in v['position_accuracy']])
 run('G_baseline', [0], 1, None, 3)
 run('G_beam_same', [0], 3, None, 3)
 run('H_threshold_ensemble', list(range(0,6)), 10, None, 3)
 run('H_threshold_ensemble_retry002', list(range(0,6)), 10, .02, 3)
 run('H_threshold_ensemble_retry005', list(range(0,6)), 10, .05, 3)
 run('I_shift_ensemble', [0,6,7,8,9], 10, .02, 3)
 run('I_all_ensemble', list(range(len(configs))), 10, .02, 3)
 run('I_all_top5', list(range(len(configs))), 10, .02, 5)
 out={'dataset':{'sources':len(rs),'training_sources':len(train),'validation_sources':len(val),'seed':41,'validation_not_used_for_templates':True},'elapsed_seconds':time.time()-start,'results':results}
 (ROOT/'second_bot/models/type1_exact_experiment_raw.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
 print('RAW',json.dumps({'elapsed_seconds':out['elapsed_seconds'],'experiments':len(results)}))
if __name__=='__main__':main()
