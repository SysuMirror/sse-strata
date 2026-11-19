#!/usr/bin/env python3
"""经网关打 8 路混合（4 快道 + 4 普道），客户端计时并读 infer.db 的落道。"""
import json, sys, threading, time, urllib.request
BASE="http://127.0.0.1:8080"
KEY=open("/home/cloud/logs/strata-api-key.txt").read().strip()
def one(model, salt, mt=128):
    body={"model":model,"messages":[{"role":"user","content":f"混合压测 {model} {salt} "+"材料。"*600}],
          "max_tokens":mt,"temperature":0.6,"stream":True}
    req=urllib.request.Request(BASE+"/v1/chat/completions",data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json","Authorization":"Bearer "+KEY})
    t0=time.time(); ttft=None; n=0; last=t0
    try:
        with urllib.request.urlopen(req,timeout=300) as r:
            for raw in r:
                line=raw.decode("utf-8","ignore").strip()
                if not line.startswith("data:"): continue
                d=line[5:].strip()
                if d=="[DONE]": break
                try: o=json.loads(d)
                except Exception: continue
                ch=(o.get("choices") or [{}])[0].get("delta") or {}
                if ch.get("content") or ch.get("reasoning_content"):
                    now=time.time()
                    if ttft is None: ttft=now-t0
                    last=now; n+=1
    except Exception as e: return {"model":model,"salt":salt,"error":repr(e)}
    return {"model":model,"salt":salt,"ttft":ttft,"total":last-t0,"tokens":n}
out=[]
ths=[]
salt0=int(time.time())
for i in range(4):
    for m in ("qwen3.8-flash-fast","qwen3.8-flash"):
        t=threading.Thread(target=lambda m=m,i=i: out.append(one(m,salt0+i*10+(0 if m.endswith("fast") else 1))))
        t.start(); ths.append(t)
t0=time.time()
for t in ths: t.join()
wall=time.time()-t0
ok=[x for x in out if "error" not in x]
print(json.dumps({"wall_s":round(wall,2),"agg_tok_s":round(sum(x['tokens'] for x in ok)/wall,2),
 "ttft_ms_mean":round(sum(x['ttft'] for x in ok if x['ttft'])/max(1,len(ok))*1000,1),
 "errors":len(out)-len(ok)},ensure_ascii=False))
