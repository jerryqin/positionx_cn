from __future__ import annotations
import datetime as dt, hashlib, json, os, re, subprocess, uuid
from pathlib import Path

def now_iso(): return dt.datetime.now().astimezone().isoformat(timespec="seconds")
def sha256_bytes(b): return hashlib.sha256(b).hexdigest()
def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()
def write_json_atomic(path,obj):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)
def extract_json(text):
    lines=text.splitlines()
    for i in range(len(lines)-1,-1,-1):
        if lines[i].lstrip().startswith("{"):
            try:return json.loads("\n".join(lines[i:]))
            except Exception:pass
    return None
def norm_date(value):
    s=str(value).replace("-","")[:8]
    if not re.fullmatch(r"\d{8}",s): raise ValueError(f"invalid date: {value}")
    return s
def calendar_date_dash(yyyymmdd):
    d=norm_date(yyyymmdd); return f"{d[:4]}-{d[4:6]}-{d[6:]}"
def qmt_trading_date_to_yyyymmdd(value):
    if isinstance(value,dt.datetime):
        return value.astimezone().strftime("%Y%m%d") if value.tzinfo else value.strftime("%Y%m%d")
    if isinstance(value,dt.date): return value.strftime("%Y%m%d")
    s=str(value).strip()
    m=re.search(r"((?:19|20)\d{2})[-/]?(\d{2})[-/]?(\d{2})",s)
    if m:
        token="".join(m.groups())
        try:dt.datetime.strptime(token,"%Y%m%d");return token
        except ValueError:pass
    try:n=float(s)
    except (TypeError,ValueError):return None
    if n!=n:return None
    a=abs(n)
    if a>=1e12:seconds=n/1000.0
    elif a>=1e9:seconds=n
    else:return None
    try:
        local_tz=dt.datetime.now().astimezone().tzinfo
        return dt.datetime.fromtimestamp(seconds,tz=dt.timezone.utc).astimezone(local_tz).strftime("%Y%m%d")
    except (OverflowError,OSError,ValueError):return None
def local_cn_exchange_is_trading_day(yyyymmdd):
    d=norm_date(yyyymmdd)
    p=Path(__file__).resolve().parents[1]/"config"/"ops"/"cn_exchange_calendar_2026.json"
    if not p.exists(): return None
    cfg=json.loads(p.read_text(encoding="utf-8"))
    if not (str(cfg.get("valid_from") or "")<=d<=str(cfg.get("valid_to") or "")): return None
    day=dt.datetime.strptime(d,"%Y%m%d")
    if bool(cfg.get("weekend_closed",True)) and day.weekday()>=5: return False
    for r in cfg.get("closed_ranges") or []:
        if str(r.get("start") or "")<=d<=str(r.get("end") or ""): return False
    return True
def qmt_is_trading_day(yyyymmdd,market="SH"):
    d=norm_date(yyyymmdd)
    local=local_cn_exchange_is_trading_day(d)
    if local is not None: return local
    from core.qmt_runtime import get_xtdata
    xtdata = get_xtdata()
    xtdata.connect()
    rows=xtdata.get_trading_dates(market,d,d)
    norm={x for x in (qmt_trading_date_to_yyyymmdd(v) for v in rows or []) if x}
    return d in norm

class TaskLock:
    def __init__(self,root,task_id,business_date):
        self.path=Path(root)/f"{task_id}_{norm_date(business_date)}.lock"; self.fd=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.fd=os.open(str(self.path),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        os.write(self.fd,f"pid={os.getpid()} started={now_iso()}\n".encode()); return self
    def __exit__(self,*exc):
        if self.fd is not None: os.close(self.fd)
        try:self.path.unlink()
        except FileNotFoundError:pass

class RunStore:
    def __init__(self,run_root): self.root=Path(run_root)
    def date_dir(self,business_date): return self.root/norm_date(business_date)
    def list_runs(self,business_date,task_id=None):
        d=self.date_dir(business_date)
        if not d.exists(): return []
        out=[]
        for p in sorted(d.glob("*.json")):
            try:o=json.loads(p.read_text(encoding="utf-8"))
            except Exception:continue
            if task_id is None or o.get("task_id")==task_id:out.append(o)
        return out
    def successful(self,business_date,task_id):
        return any(x.get("status")=="SUCCESS" for x in self.list_runs(business_date,task_id))
    def latest(self,business_date,task_id):
        xs=self.list_runs(business_date,task_id); return xs[-1] if xs else None
    def new(self,task_id,business_date,scheduled_source):
        rid=f"{norm_date(business_date)}_{task_id}_{dt.datetime.now().strftime('%H%M%S')}_{uuid.uuid4().hex[:8]}"
        obj={"schema":"POSITIONX_CN_OPS_RUN_V1","run_id":rid,"task_id":task_id,"business_date":norm_date(business_date),
             "scheduled_source":scheduled_source,"scheduled_at":None,"actual_started_at":now_iso(),"finished_at":None,
             "status":"RUNNING","exit_code":None,"retry_no":len(self.list_runs(business_date,task_id)),
             "dependency_state":{},"input_trade_date":norm_date(business_date),"command_identity":None,
             "code_or_config_sha256":None,"output_artifacts":[],"output_sha256":[],"log_path":None,
             "error_summary":None,"parsed_summary":None}
        p=self.date_dir(business_date)/f"{rid}.json"; write_json_atomic(p,obj); return p,obj
    def finish(self,path,obj,status,**updates):
        obj.update(updates); obj["status"]=status; obj["finished_at"]=now_iso(); write_json_atomic(path,obj); return obj

def command_forbidden(argv,registry):
    low=[str(x).lower() for x in argv]; joined=" ".join(low)
    for script in registry["forbidden_automation"]["script_names"]:
        if script.lower() in joined:return f"forbidden script: {script}"
    for token in registry["forbidden_automation"]["argv_tokens"]:
        if token.lower() in low:return f"forbidden argv token: {token}"
    for cmd in registry["forbidden_automation"]["exact_commands"]:
        if all(str(x).lower() in low for x in cmd):return f"forbidden exact command: {cmd}"
    return None
def render_argv(argv,project_root,business_date):
    py=str(Path(project_root)/".venv"/"Scripts"/"python.exe")
    rep={"<python>":py,"<trade_date>":norm_date(business_date),"<calendar_date>":calendar_date_dash(business_date),"<project_root>":str(project_root)}
    return [rep.get(str(x),str(x).replace("<trade_date>",norm_date(business_date)).replace("<calendar_date>",calendar_date_dash(business_date)).replace("<project_root>",str(project_root))) for x in argv]
def run_process(argv,cwd,log_path,env=None):
    log_path=Path(log_path); log_path.parent.mkdir(parents=True,exist_ok=True)
    e=os.environ.copy()
    if env:e.update(env)
    cp=subprocess.run(list(argv),cwd=str(cwd),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",check=False,env=e)
    log_path.write_text(cp.stdout,encoding="utf-8")
    return {"returncode":cp.returncode,"stdout":cp.stdout,"parsed_json":extract_json(cp.stdout),"log_path":str(log_path)}
