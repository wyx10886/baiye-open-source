"""Scoped user-choice observations. Inferred habits never become facts by repetition.

Only filtered short user excerpts enter this store. The active host model may
normalise their meaning with observe using a matching, expiring receipt.
No transcript crawler, background model or external service runs.
enabled controls collection; use_learned independently controls reuse.
The caller must verify native user/thread identity before capture_prompt.
"""
from __future__ import annotations
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from .store import ConflictError, SECRET, ValidationError, _scope, _text, _tokens

_DEFAULTS = {"enabled": False, "use_learned": False, "allow_tool_assisted": False}
_ACTIVE = ("candidate", "learned", "conflicted", "confirmed")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, clearing TEXT);
INSERT OR IGNORE INTO control VALUES(1,NULL);
CREATE TABLE IF NOT EXISTS policies (
 id TEXT PRIMARY KEY, settings TEXT NOT NULL, source TEXT NOT NULL,
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
 id TEXT PRIMARY KEY, turn_key TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
 scope TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, excerpts TEXT NOT NULL, external_context INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS seen_turns (turn_key TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS habits (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, topic TEXT NOT NULL,
 topic_key TEXT NOT NULL, value TEXT NOT NULL, value_key TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL, memory_id TEXT,
 memory_root_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 pending_action TEXT, pending_token TEXT, UNIQUE(scope,topic_key,value_key));
CREATE TABLE IF NOT EXISTS evidence (
 habit_id TEXT NOT NULL, receipt_id TEXT NOT NULL, turn_key TEXT NOT NULL,
 thread_id TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
 quote TEXT NOT NULL, PRIMARY KEY(habit_id,turn_key));
CREATE TABLE IF NOT EXISTS suppressed (signature TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppressed_roots (root_id TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS habit_scope_state ON habits(scope,state);
CREATE INDEX IF NOT EXISTS habit_root ON habits(memory_root_id);
CREATE INDEX IF NOT EXISTS evidence_habit ON evidence(habit_id);
PRAGMA user_version=1;
"""
_SIGNAL = re.compile(
 r"我(?:更|一般|通常|平时|还是|一直|比较|习惯|想|希望|喜欢|倾向)|我的(?:习惯|偏好)|我(?:不|更不|并不)(?:喜欢|想|希望|习惯)|默认|以后|今后|每次|总是|照旧|"
 r"还是(?:用|给|按)|改成|改用|换成|\bI\s+(?:prefer|like|usually|always|tend|want)|"
 r"\b(?:please always|from now on|as usual|prefer)\b", re.I)
_TEMPORARY = re.compile(r"这次|这篇|本次|这一个|今天|暂时|目前|仅(?:此|这)|只(?:在|针对)(?:本|这)|"
 r"\b(?:just this|for this task|this time|today|temporarily|this paper)\b", re.I)
_EXCLUDED = re.compile(
 r"虚构|角色|扮演|病娇|故事里|剧情|我(?:朋友|同学|师兄|导师|同事|妈妈|爸爸)|"
 r"(?:他|她|别人|对方)(?:说|喜欢|希望|要求)|引用|转述|示例文本|测试文本|假设用户|"
 r"身份证|银行卡|宗教|性取向|疾病|诊断|HIV|\b(?:roleplay|fictional|pretend|"
 r"my friend|my colleague|he says|she says|they prefer|sample text|quoted text)\b", re.I)
_CONTROL = re.compile(r"^\s*(?:\[local-(?:proactive-chat|housekeeping)\]|<heartbeat\b|"
 r"<environment_context\b|#\s*AGENTS\.md instructions|<send_user_message_question_reply>)", re.I)
_TOPIC_ALIASES = {"paper.comments":"document.review-method", "paper.review-method":"document.review-method",
 "document.comments":"document.review-method", "answer.length":"response.length",
 "reply.length":"response.length", "folder.reuse":"workflow.directory"}
_VALUE_ALIASES = {
 "document.review-method":{"批注":"文档内批注", "评论":"文档内批注", "comments":"文档内批注",
 "annotations":"文档内批注", "inline comments":"文档内批注", "直接修改":"直接修改正文", "direct edits":"直接修改正文"},
 "response.length":{"简短":"简洁回答", "简洁":"简洁回答", "brief":"简洁回答", "concise":"简洁回答",
 "详细":"详细解释", "detailed":"详细解释"},
 "document.format":{"docx":"Word", "word":"Word", "md":"Markdown", "markdown":"Markdown", "pdf":"PDF"}}

def _now():
    return datetime.now(timezone.utc)

def _stamp():
    return _now().isoformat(timespec="microseconds")

def _digest(*values):
    return hashlib.sha256("\0".join(str(v) for v in values).encode("utf-8")).hexdigest()

def _canonical(topic, value):
    topic = _text(topic, "topic", 80).casefold()
    if re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", topic) is None:
        raise ValidationError("topic must be a stable ASCII label, such as response.length")
    topic = _TOPIC_ALIASES.get(topic, topic)
    value = _text(value, "value", 400)
    value = _VALUE_ALIASES.get(topic, {}).get(value.casefold(), value)
    return topic, value, _digest(topic), _digest(re.sub(r"\s+", " ", value).casefold())

def _excerpts(prompt):
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>100_000:
        return []
    if _CONTROL.search(prompt) or SECRET.search(prompt):
        return []
    if "## My request:" in prompt:
        prompt = prompt.split("## My request:",1)[1]
    prompt = re.split(r"<image\b",prompt,maxsplit=1,flags=re.I)[0]
    prompt = re.sub(r"```.*?```|~~~.*?~~~"," ",prompt,flags=re.S)
    prompt = re.sub(r"<[^>]+>.*?</[^>]+>"," ",prompt,flags=re.S)
    prompt = re.sub(r"`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\""," ",prompt)
    result=[]
    for line in prompt.splitlines():
        if line.lstrip().startswith((">","#","- [","* [")):
            continue
        for sentence in re.split(r"(?<=[。！？!?])|\n",line):
            sentence=sentence.strip(" \t-*•")
            if not 6<=len(sentence)<=360:
                continue
            if not _SIGNAL.search(sentence) or _TEMPORARY.search(sentence) or _EXCLUDED.search(sentence):
                continue
            if SECRET.search(sentence) or re.search(r"https?://|\b(?:system|developer)\s*:",sentence,re.I):
                continue
            if sentence not in result:
                result.append(sentence)
            if len(result)==6:
                return result
    return result

def _classify(quote):
    """Transparent baseline; other choices may be normalised by the active host model."""
    pairs=[]
    def add(topic,value):
        if (topic,value) not in pairs:
            pairs.append((topic,value))
    q=quote.casefold()
    # Negation and changes of mind require semantic interpretation, not a keyword guess.
    if re.search(r"不要|不用|不想|不喜欢|不再|不是|而不是|但是|别|\b(?:not|never|rather than|instead of|don.t|do not)\b",q):
        return []
    if re.search(r"回答|回复|解释|讲解|说明|\b(?:answers?|responses?|repl(?:y|ies)|explain)\b",q):
        brief=bool(re.search(r"简短|简洁|精炼|少说|短一点|\b(?:concise|brief|short)\b",q))
        detailed=bool(re.search(r"详细|展开|讲细|一步步|\b(?:detailed|step.by.step|in depth)\b",q))
        if brief!=detailed:
            add("response.length","简洁回答" if brief else "详细解释")
        if re.search(r"先.{0,6}(?:结论|结果|要点)|(?:conclusion|answer|result).{0,8}first",q):
            add("response.structure","先给结论再解释")
    if re.search(r"论文|文档|稿件|稿子|\b(?:papers?|documents?|manuscripts?)\b",q):
        comment=bool(re.search(r"批注|评论|\b(?:comments?|annotations?)\b",q))
        direct=bool(re.search(r"直接(?:改|修改|编辑)|\bdirect(?:ly)? edit",q))
        if comment and re.search(r"(?:不要|不用|不加|不写).{0,4}(?:批注|评论)|no comments?",q):
            comment=False
        if comment!=direct:
            add("document.review-method","文档内批注" if comment else "直接修改正文")
        formats=[v for p,v in ((r"\bword\b|docx","Word"),(r"\bpdf\b","PDF"),
                 (r"\bmarkdown\b|\.md\b","Markdown")) if re.search(p,q)]
        if len(formats)==1:
            add("document.format",formats[0])
    if re.search(r"目录|文件夹|\b(?:folders?|director(?:y|ies))\b",q):
        if re.search(r"复用|沿用|同一个|原(?:来|有|本)?(?:的)?(?:目录|文件夹)|reuse|existing",q):
            add("workflow.directory","复用已有任务目录")
        elif re.search(r"每.{0,8}(?:另建|新建|单独)|new.{0,8}(?:folder|directory)",q):
            add("workflow.directory","每项任务单独新建目录")
    if re.search(r"代码|示例|\b(?:code|examples?)\b",q):
        if re.search(r"(?:可|能|直接)运行|完整示例|runnable|working example",q):
            add("code.examples","提供可运行的代码示例")
        elif re.search(r"伪代码|pseudocode",q):
            add("code.examples","使用伪代码解释")
    if re.search(r"中文|chinese",q) and re.search(r"回答|回复|解释|reply|answer|respond",q):
        add("communication.language","中文")
    elif re.search(r"英文|english",q) and re.search(r"回答|回复|解释|reply|answer|respond",q):
        add("communication.language","英文")
    if re.search(r"图解|关系图|流程图|diagram|visual explanation",q):
        add("learning.presentation","优先用图解说明关系")
    return pairs

def _operation_token():
    return json.dumps({"id":str(uuid.uuid4()),"pid":os.getpid(),"at":_stamp()},separators=(",",":"))

def _owner_alive(token):
    """Unknown ownership stays busy. Only a proved exited process may be recovered."""
    try:
        pid=json.loads(token)["pid"]
        if type(pid) is not int or pid<=0 or pid>0xffffffff:
            return True
    except (ValueError,TypeError,KeyError):
        return True
    if pid==os.getpid():
        return True
    if os.name=="nt":
        import ctypes
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return ctypes.get_last_error()!=87
        try:
            code=ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError,OverflowError,OSError):
        return True

class HabitLearning:
    def __init__(self,home):
        self.home=Path(home).expanduser().resolve()
        self.db=self.home/"habits.sqlite3"

    @contextmanager
    def _connect(self):
        self.home.mkdir(parents=True,exist_ok=True)
        con=sqlite3.connect(self.db,timeout=.5)
        try:
            con.row_factory=sqlite3.Row
            con.execute("PRAGMA busy_timeout=500")
            con.execute("PRAGMA secure_delete=ON")
            version=con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0,1):
                raise ValidationError("Unsupported habit database version")
            if version==0:
                con.executescript(_SCHEMA)
            self._recover_dead(con)
            with con:
                yield con
        finally:
            con.close()

    def _recover_dead(self,con):
        rows=[dict(r) for r in con.execute("SELECT * FROM habits WHERE pending_action IS NOT NULL")
              if not _owner_alive(r["pending_token"])]
        clearing=con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0]
        clear_dead=bool(clearing and not _owner_alive(clearing))
        if not rows and not clear_dead:
            return
        con.execute("BEGIN IMMEDIATE")
        try:
            for old in rows:
                current=con.execute("SELECT * FROM habits WHERE id=?",(old["id"],)).fetchone()
                if not current or current["pending_token"]!=old["pending_token"]:
                    continue
                owned=self._owned_memory(old["memory_root_id"])
                if not owned and not old["memory_root_id"] and (self.home/"memory.sqlite3").exists():
                    with closing(sqlite3.connect((self.home/"memory.sqlite3").resolve().as_uri()+"?mode=ro",uri=True,timeout=.5)) as mem:
                        mem.row_factory=sqlite3.Row
                        found=mem.execute("SELECT * FROM memories WHERE key=? AND state='active'",
                                          ("learned-habit."+old["id"],)).fetchone()
                        owned=dict(found) if found else None
                action=old["pending_action"]
                if action=="confirm" and owned and owned["status"]=="confirmed" and owned["content"]==old["value"]:
                    con.execute("UPDATE habits SET state='confirmed',memory_id=?,memory_root_id=? WHERE id=?",
                                (owned["id"],owned["root_id"],old["id"]))
                elif action=="edit" and owned and owned["id"]!=old["memory_id"]:
                    _,value,_,value_key=_canonical(old["topic"],owned["content"])
                    con.execute("DELETE FROM receipts WHERE id IN (SELECT receipt_id FROM evidence WHERE habit_id=?)",(old["id"],))
                    con.execute("DELETE FROM evidence WHERE habit_id=?",(old["id"],))
                    con.execute("UPDATE habits SET state='candidate',value=?,value_key=?,memory_id=? WHERE id=?",
                                (value,value_key,owned["id"],old["id"]))
                elif action in {"forget","reject"} and not owned:
                    self._purge_con(con,current,"rejected" if action=="reject" else "forgotten")
                con.execute("UPDATE habits SET pending_action=NULL,pending_token=NULL,revision=revision+1,updated_at=? WHERE id=?",
                            (_stamp(),old["id"]))
            if clear_dead:
                con.execute("UPDATE control SET clearing=NULL WHERE id=1 AND clearing=?",(clearing,))
            con.commit()
        except Exception:
            con.rollback()
            raise

    @staticmethod
    def _policy(con,thread_id=None):
        def read(key):
            row=con.execute("SELECT * FROM policies WHERE id=?",(key,)).fetchone()
            return (json.loads(row["settings"]),row["revision"]) if row else ({},0)
        global_values,global_revision=read("global")
        values=dict(_DEFAULTS,**global_values)
        revision=global_revision
        if thread_id:
            local,revision=read("thread:"+thread_id)
            for key in _DEFAULTS:
                values[key]=values[key] and local.get(key,True)
        return dict(values,revision=revision,global_revision=global_revision,thread_id=thread_id,
                    enabled_controls="new observations only; use_learned is independent")

    def status(self,thread_id=None):
        if thread_id is not None:
            thread_id=_text(thread_id,"thread_id",200)
        if not self.db.exists():
            return {"policy":dict(_DEFAULTS,revision=0,global_revision=0,thread_id=thread_id),
                    "counts":{},"database":str(self.db)}
        with self._connect() as con:
            counts={r["state"]:r["n"] for r in con.execute("SELECT state,COUNT(*) n FROM habits GROUP BY state")}
            counts["suppressed"]=con.execute("SELECT COUNT(*) FROM suppressed").fetchone()[0]
            pending=[{"id":r["id"],"action":r["pending_action"]} for r in con.execute(
                "SELECT id,pending_action FROM habits WHERE pending_action IS NOT NULL")]
            return {"policy":self._policy(con,thread_id),"counts":counts,"database":str(self.db),
                    "pending":pending,"clearing":bool(con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0])}

    def configure(self,*,source,enabled=None,use_learned=None,allow_tool_assisted=None,
                  thread_id=None,expected_revision=None):
        source=_text(source,"source",1000)
        if thread_id is not None:
            thread_id=_text(thread_id,"thread_id",200)
        updates={k:v for k,v in {"enabled":enabled,"use_learned":use_learned,
                 "allow_tool_assisted":allow_tool_assisted}.items() if v is not None}
        if not updates or any(type(v) is not bool for v in updates.values()):
            raise ValidationError("Provide one or more boolean policy changes")
        if expected_revision is not None and type(expected_revision) is not int:
            raise ValidationError("expected_revision must be an integer")
        key="thread:"+thread_id if thread_id else "global"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT * FROM policies WHERE id=?",(key,)).fetchone()
            revision=row["revision"] if row else 0
            if expected_revision is not None and expected_revision!=revision:
                raise ConflictError("Habit policy changed; reload before configuring")
            settings=dict(json.loads(row["settings"]) if row else {},**updates)
            con.execute("INSERT OR REPLACE INTO policies VALUES(?,?,?,?,?)",
                        (key,json.dumps(settings),source,revision+1,_stamp()))
        return self.status(thread_id)

    def capture_prompt(self,prompt,*,thread_id,turn_id=None,project=None,
                       source="Codex UserPromptSubmit",external_context=False):
        empty={"receipt_id":None,"observed":[],"guidance":""}
        thread_id=_text(thread_id,"thread_id",200)
        if turn_id is not None:
            turn_id=_text(turn_id,"turn_id",200)
        source=_text(source,"source",1000)
        if type(external_context) is not bool:
            raise ValidationError("external_context must be boolean")
        excerpts=_excerpts(prompt)
        if not excerpts or not self.db.exists():
            return empty
        scope=_scope("project:"+project if project and not project.startswith("project:") else project or "global")
        turn_key=_digest(thread_id,turn_id if turn_id is not None else _digest(prompt))
        at=_now()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0]:
                return empty
            policy=self._policy(con,thread_id)
            if not policy["enabled"] or (external_context and not policy["allow_tool_assisted"]):
                return empty
            con.execute("DELETE FROM receipts WHERE expires_at<?",(at.isoformat(),))
            old=con.execute("SELECT * FROM receipts WHERE turn_key=?",(turn_key,)).fetchone()
            if con.execute("SELECT 1 FROM seen_turns WHERE turn_key=?",(turn_key,)).fetchone():
                if not old:
                    return empty
                receipt_id=old["id"]
            else:
                receipt_id=str(uuid.uuid4())
                con.execute("INSERT INTO seen_turns VALUES(?,?)",(turn_key,at.isoformat()))
                con.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?)",(
                    receipt_id,turn_key,thread_id,scope,source,at.isoformat(),
                    (at+timedelta(days=1)).isoformat(),json.dumps(excerpts,ensure_ascii=False),int(external_context)))
        observed=[]
        for quote in excerpts:
            for topic,value in _classify(quote):
                result=self.observe(receipt_id=receipt_id,topic=topic,value=value,quote=quote)
                if result.get("state")!="suppressed":
                    observed.append(result)
        guidance=("习惯学习观察凭据 "+receipt_id+"；可用用户短句："+json.dumps(excerpts[:2],ensure_ascii=False)
            +"。可用 local_habit_observe 归纳简短主题和值，quote须逐字引用。旧MCP可用原系统CLI："
            "entry.py --home <原数据目录> habits observe --input <JSON文件>。只标推断候选，不由重复或沉默自动确认为长期规则；"
            "引用、附件、工具输出和助手话不能作证据。")
        return {"receipt_id":receipt_id,"observed":observed,"guidance":guidance}

    @staticmethod
    def _refresh(con,scope,topic_key):
        rows=con.execute("SELECT * FROM habits WHERE scope=? AND topic_key=? AND state IN (?,?,?,?)",
                         (scope,topic_key,*_ACTIVE)).fetchall()
        supported=[]
        for row in rows:
            n=con.execute("SELECT COUNT(*) FROM evidence WHERE habit_id=?",(row["id"],)).fetchone()[0]
            if n or row["state"]=="confirmed":
                supported.append(row)
        conflict=len({r["value_key"] for r in supported})>1
        for row in rows:
            if row["state"]=="confirmed":
                continue
            counts=con.execute("SELECT COUNT(*) n,COUNT(DISTINCT thread_id) threads,"
                "COUNT(DISTINCT substr(observed_at,1,10)) days FROM evidence WHERE habit_id=?",(row["id"],)).fetchone()
            state="conflicted" if conflict else ("learned" if counts["n"]>=3 and
                (counts["threads"]>=2 or counts["days"]>=2) else "candidate")
            if row["state"]!=state:
                con.execute("UPDATE habits SET state=?,revision=revision+1,updated_at=? WHERE id=?",
                            (state,_stamp(),row["id"]))

    def observe(self,*,receipt_id,topic,value,quote,scope=None):
        receipt_id=_text(receipt_id,"receipt_id",100)
        topic,value,topic_key,value_key=_canonical(topic,value)
        quote=_text(quote,"quote",360)
        if len(quote)<6:
            raise ValidationError("quote must include at least six attributable characters")
        if not self.db.exists():
            raise ValidationError("Observation receipt not found")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0]:
                raise ConflictError("Habit data is being cleared; retry afterwards")
            receipt=con.execute("SELECT * FROM receipts WHERE id=?",(receipt_id,)).fetchone()
            if not receipt or datetime.fromisoformat(receipt["expires_at"])<=_now():
                raise ValidationError("Observation receipt is missing or expired")
            policy=self._policy(con,receipt["thread_id"])
            if not policy["enabled"] or (receipt["external_context"] and not policy["allow_tool_assisted"]):
                raise ValidationError("Habit learning is paused for this receipt")
            if scope is not None and _scope(scope)!=receipt["scope"]:
                raise ValidationError("Observation scope must match the verified receipt")
            scope=receipt["scope"]
            if not any(quote in part for part in json.loads(receipt["excerpts"])):
                raise ValidationError("quote does not match attributable text in this receipt")
            if _EXCLUDED.search(quote) or SECRET.search(quote):
                raise ValidationError("Excluded observation material")
            signature=_digest(scope,topic_key,value_key)
            if con.execute("SELECT 1 FROM suppressed WHERE signature=?",(signature,)).fetchone():
                return {"state":"suppressed","status":"candidate"}
            if con.execute("SELECT 1 FROM habits WHERE scope=? AND topic_key=? AND pending_action IS NOT NULL LIMIT 1",
                           (scope,topic_key)).fetchone():
                raise ConflictError("This habit topic is being reviewed; retry afterwards")
            row=con.execute("SELECT * FROM habits WHERE scope=? AND topic_key=? AND value_key=?",
                            (scope,topic_key,value_key)).fetchone()
            if row and row["state"] not in _ACTIVE:
                return {"state":"suppressed","status":"candidate"}
            if row and row["pending_action"]:
                raise ConflictError("Habit is being reviewed; retry after review completes")
            if row is None:
                habit_id=str(uuid.uuid4())
                con.execute("INSERT INTO habits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                    habit_id,scope,topic,topic_key,value,value_key,"candidate",1,
                    None,None,_stamp(),_stamp(),None,None))
            else:
                habit_id=row["id"]
            changed=con.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)",(
                habit_id,receipt_id,receipt["turn_key"],receipt["thread_id"],receipt["source"],
                receipt["created_at"],quote)).rowcount
            if changed and row is not None:
                con.execute("UPDATE habits SET revision=revision+1,updated_at=? WHERE id=?",(_stamp(),habit_id))
            self._refresh(con,scope,topic_key)
        return self.get(habit_id)

    def _view(self,con,row,*,evidence=False):
        item=dict(row)
        entries=[dict(r) for r in con.execute("SELECT thread_id,source,observed_at,quote,receipt_id FROM evidence "
            "WHERE habit_id=? ORDER BY observed_at",(row["id"],))]
        count=len(entries)
        threads=len({e["thread_id"] for e in entries})
        days=len({e["observed_at"][:10] for e in entries})
        confidence=min(.9,.35+.1*min(count,5)+.03*max(0,threads-1)+.02*max(0,days-1))
        if row["state"]=="conflicted":
            confidence=min(confidence,.3)
        item.update(status="confirmed" if row["state"]=="confirmed" else "candidate",
            confidence=round(confidence if count else 0,2),evidence_count=count,thread_count=threads,day_count=days)
        item.pop("pending_token",None)
        item.pop("topic_key",None)
        item.pop("value_key",None)
        if row["state"] in _ACTIVE and row["memory_root_id"] and (self.home/"memory.sqlite3").exists():
            with closing(sqlite3.connect((self.home/"memory.sqlite3").resolve().as_uri()+"?mode=ro",uri=True,timeout=.5)) as memory:
                memory.row_factory=sqlite3.Row
                current=memory.execute("SELECT id,revision,content,status,state FROM memories "
                    "WHERE root_id=? AND state='active' ORDER BY revision DESC LIMIT 1",(row["memory_root_id"],)).fetchone()
            if current:
                item["memory_id"]=current["id"]
                if current["id"]!=row["memory_id"] or current["content"]!=row["value"]:
                    item.update(value=current["content"],external_revision=True,evidence_count=0,
                                confidence=0,status=current["status"])
                    entries=[]
            else:
                with closing(sqlite3.connect((self.home/"memory.sqlite3").resolve().as_uri()+"?mode=ro",uri=True,timeout=.5)) as memory:
                    deleted=memory.execute("SELECT 1 FROM memories WHERE root_id=? AND state='deleted' LIMIT 1",
                                           (row["memory_root_id"],)).fetchone()
                if deleted:
                    self._purge_con(con,row)
                    item.update(value="",topic="[已遗忘]",state="forgotten",status="candidate",
                                revision=row["revision"]+1,evidence_count=0,confidence=0)
                else:
                    item.update(value="",external_revision=True,evidence_count=0,confidence=0)
                entries=[]
        if evidence:
            item["evidence"]=entries
        return item

    def list(self,*,scope=None,include_inactive=False,limit=30):
        if type(limit) is not int or not 1<=limit<=200:
            raise ValidationError("limit must be from 1 to 200")
        if type(include_inactive) is not bool:
            raise ValidationError("include_inactive must be boolean")
        if scope is not None:
            scope=_scope(scope)
        if not self.db.exists():
            return []
        with self._connect() as con:
            clauses,args=[],[]
            if scope is not None:
                clauses.append("scope=?");args.append(scope)
            if not include_inactive:
                clauses.append("state IN (?,?,?,?)");args.extend(_ACTIVE)
            sql="SELECT * FROM habits"+(" WHERE "+" AND ".join(clauses) if clauses else "")
            return [self._view(con,r) for r in con.execute(sql+" ORDER BY updated_at DESC,id LIMIT ?",args+[limit])]

    def get(self,habit_id):
        habit_id=_text(habit_id,"habit_id",100)
        if not self.db.exists():
            raise ValidationError("Habit not found")
        with self._connect() as con:
            row=con.execute("SELECT * FROM habits WHERE id=?",(habit_id,)).fetchone()
            if not row:
                raise ValidationError("Habit not found")
            return self._view(con,row,evidence=True)

    def context(self,query="",*,project=None,thread_id=None,budget=1500):
        if type(budget) is not int or not 0<=budget<=6000:
            raise ValidationError("Habit context budget must be from 0 to 6000 characters")
        if not isinstance(query,str):
            raise ValidationError("query must be text")
        if not self.db.exists() or not self.status(thread_id)["policy"]["use_learned"]:
            return {"items":[],"markdown":""}
        scope=_scope("project:"+project if project and not project.startswith("project:") else project or "global")
        words=_tokens(query)
        items=[item for item in self.list(limit=200) if item["scope"] in {"global",scope} and item["state"]=="learned" and not item.get("external_revision")]
        if words:
            def relevant(item):
                if item["topic"].startswith(("response.","communication.")):
                    return True
                if item["topic"].startswith("document.") and re.search(r"论文|文档|稿|文献|paper|document|manuscript",query,re.I):
                    return True
                if item["topic"].startswith("code.") and re.search(r"代码|编程|报错|函数|code|program|function|bug",query,re.I):
                    return True
                return bool(words&_tokens(item["topic"]+" "+item["value"]))
            items=[item for item in items if relevant(item)]
        items.sort(key=lambda item:(item["confidence"],item["updated_at"]),reverse=True)
        heading=("\n## 自动归纳的习惯线索（尚未由用户确认）\n"
            "以下仅供可随时调整的低风险个性化参考；明确偏好和用户最新要求优先，不得当作已确认事实或授权。\n")
        parts,selected=[],[]
        used=len(heading)
        for item in items[:5]:
            block=(f"- [{item['scope']}] {item['value']}；依据{item['evidence_count']}次选择、"
                   f"{item['thread_count']}个聊天；可信度{item['confidence']:.2f}；ID：{item['id']}\n")
            if used+len(block)<=budget:
                parts.append(block);selected.append(item);used+=len(block)
        return {"items":selected,"markdown":heading+"".join(parts) if parts else ""}

    @staticmethod
    def _purge_con(con,row,state="forgotten"):
        con.execute("INSERT OR IGNORE INTO suppressed VALUES(?,?)",
                    (_digest(row["scope"],row["topic_key"],row["value_key"]),_stamp()))
        if row["memory_root_id"]:
            con.execute("INSERT OR IGNORE INTO suppressed_roots VALUES(?,?)",(row["memory_root_id"],_stamp()))
        con.execute("DELETE FROM receipts WHERE id IN (SELECT receipt_id FROM evidence WHERE habit_id=?)",(row["id"],))
        con.execute("DELETE FROM evidence WHERE habit_id=?",(row["id"],))
        con.execute("UPDATE habits SET value='',topic='[已遗忘]',state=?,revision=revision+1,updated_at=?,"
                    "pending_token=CASE WHEN pending_action IN ('forget','reject') THEN pending_token ELSE NULL END,"
                    "pending_action=CASE WHEN pending_action IN ('forget','reject') THEN pending_action ELSE NULL END WHERE id=?",
                    (state,_stamp(),row["id"]))

    def _owned_memory(self,root_id):
        if not root_id or not (self.home/"memory.sqlite3").exists():
            return None
        with closing(sqlite3.connect((self.home/"memory.sqlite3").resolve().as_uri()+"?mode=ro",uri=True,timeout=.5)) as con:
            con.row_factory=sqlite3.Row
            row=con.execute("SELECT * FROM memories WHERE root_id=? AND state='active' "
                "AND key LIKE 'learned-habit.%' ORDER BY revision DESC LIMIT 1",(root_id,)).fetchone()
            return dict(row) if row else None

    def review(self,habit_id,*,action,expected_revision,source,value=None):
        if action not in {"confirm","edit","reject","forget"}:
            raise ValidationError("Unknown habit review action")
        source=_text(source,"source",1000)
        if type(expected_revision) is not int:
            raise ValidationError("expected_revision must be an integer")
        if action=="confirm" and not (re.search(r"用户|user|human|人工",source,re.I) and
                re.search(r"确认|同意|要求|confirm|approv|consent",source,re.I)):
            raise ValidationError("Confirmation must cite an explicit human confirmation")
        if action=="edit":
            value=_text(value,"value",400)
        elif value is not None:
            raise ValidationError("value is only accepted for edit")
        token=_operation_token()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0]:
                raise ConflictError("Habit data is being cleared; retry afterwards")
            old=con.execute("SELECT * FROM habits WHERE id=?",(habit_id,)).fetchone()
            if not old:
                raise ValidationError("Habit not found")
            if old["revision"]!=expected_revision or old["pending_action"]:
                raise ConflictError("Habit changed; reload before reviewing")
            if old["state"] not in _ACTIVE:
                raise ValidationError("Habit is inactive")
            old=dict(old)
            if action=="edit":
                _,value,_,new_key=_canonical(old["topic"],value)
                collision=con.execute("SELECT id FROM habits WHERE scope=? AND topic_key=? AND value_key=? AND id<>?",
                    (old["scope"],old["topic_key"],new_key,habit_id)).fetchone()
                if collision:
                    raise ConflictError("Another habit already uses this value; review those records separately")
            con.execute("UPDATE habits SET pending_action=?,pending_token=?,revision=revision+1 WHERE id=?",
                        (action,token,habit_id))
        linked=None
        try:
            # Outside a habit SQLite transaction: Store may call forget_linked.
            from .store import Store
            owned=self._owned_memory(old["memory_root_id"])
            if action=="confirm":
                if owned:
                    if owned["id"]!=old["memory_id"] or owned["content"]!=old["value"]:
                        raise ConflictError("Linked memory was revised externally; review that memory directly before confirming")
                    if owned["status"]!="confirmed":
                        linked=Store(self.home).revise(owned["id"],expected_revision=owned["revision"],
                            source=source,content=old["value"],status="confirmed")
                    else:
                        linked=owned
                else:
                    linked=Store(self.home).remember(kind="preference",scope=old["scope"],status="confirmed",
                        title="已确认习惯："+old["topic"],content=old["value"],source=source,
                        key="learned-habit."+habit_id,event_date=_now().date().isoformat(),importance=4,
                        tags=["habit-learning","user-confirmed"],idempotency_key="learned-habit-confirm:"+habit_id)
                if linked["state"]!="active":
                    raise ConflictError("Linked memory was forgotten; do not recreate it")
            elif action=="edit" and owned:
                linked=Store(self.home).revise(owned["id"],expected_revision=owned["revision"],source=source,
                                               content=value,status="candidate")
            elif action in {"forget","reject"} and owned:
                Store(self.home).forget(owned["id"],expected_revision=owned["revision"],reason=source)
        except Exception:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE habits SET pending_action=NULL,pending_token=NULL,revision=revision+1 "
                            "WHERE id=? AND pending_token=?",(habit_id,token))
            raise
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT * FROM habits WHERE id=?",(habit_id,)).fetchone()
            if row["pending_token"]!=token:
                raise ConflictError("Habit review changed during the operation")
            if action in {"forget","reject"}:
                self._purge_con(con,row,"rejected" if action=="reject" else "forgotten")
            elif action=="edit":
                topic,value,topic_key,value_key=_canonical(old["topic"],value)
                con.execute("INSERT OR IGNORE INTO suppressed VALUES(?,?)",
                    (_digest(old["scope"],old["topic_key"],old["value_key"]),_stamp()))
                con.execute("DELETE FROM receipts WHERE id IN (SELECT receipt_id FROM evidence WHERE habit_id=?)",(habit_id,))
                con.execute("DELETE FROM evidence WHERE habit_id=?",(habit_id,))
                con.execute("UPDATE habits SET value=?,value_key=?,state='candidate',revision=revision+1,updated_at=? WHERE id=?",
                            (value,value_key,_stamp(),habit_id))
            elif action=="confirm":
                con.execute("UPDATE habits SET state='confirmed',memory_id=?,memory_root_id=?,revision=revision+1,updated_at=? WHERE id=?",
                            (linked["id"],linked["root_id"],_stamp(),habit_id))
            if linked and action=="edit":
                con.execute("UPDATE habits SET memory_id=?,memory_root_id=? WHERE id=?",
                            (linked["id"],linked["root_id"],habit_id))
            con.execute("UPDATE habits SET pending_action=NULL,pending_token=NULL WHERE id=?",(habit_id,))
            self._refresh(con,old["scope"],old["topic_key"])
        return self.get(habit_id)

    def forget_linked(self,memory_root_id):
        """Called after Store.forget; never opens Store or recursively deletes."""
        memory_root_id=_text(memory_root_id,"memory_root_id",100)
        if not self.db.exists():
            return {"forgotten":0}
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows=con.execute("SELECT * FROM habits WHERE memory_root_id=?",(memory_root_id,)).fetchall()
            for row in rows:
                self._purge_con(con,row)
            if rows:
                con.execute("INSERT OR IGNORE INTO suppressed_roots VALUES(?,?)",(memory_root_id,_stamp()))
        return {"forgotten":len(rows)}

    def suppressed_memory_roots(self):
        if not self.db.exists():
            return []
        with self._connect() as con:
            return [r[0] for r in con.execute("SELECT root_id FROM suppressed_roots ORDER BY root_id")]

    def clear(self,*,source,expected_revision=None):
        source=_text(source,"source",1000)
        if expected_revision is not None and type(expected_revision) is not int:
            raise ValidationError("expected_revision must be an integer")
        if not self.db.exists():
            if expected_revision not in (None,0):
                raise ConflictError("Habit policy changed; reload before clearing")
            return {"cleared":0,"linked_memories_forgotten":0}
        token=_operation_token()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if expected_revision is not None and expected_revision!=self._policy(con)["revision"]:
                raise ConflictError("Habit policy changed; reload before clearing")
            if con.execute("SELECT clearing FROM control WHERE id=1").fetchone()[0]:
                raise ConflictError("Habit data is already being cleared")
            if con.execute("SELECT 1 FROM habits WHERE pending_action IS NOT NULL LIMIT 1").fetchone():
                raise ConflictError("A habit review is in progress; retry clearing after it finishes")
            con.execute("UPDATE control SET clearing=? WHERE id=1",(token,))
            roots=[r[0] for r in con.execute("SELECT DISTINCT memory_root_id FROM habits WHERE memory_root_id IS NOT NULL")]
        linked_count=0
        try:
            from .store import Store
            for root in roots:
                owned=self._owned_memory(root)
                if owned:
                    Store(self.home).forget(owned["id"],expected_revision=owned["revision"],reason=source)
                    linked_count+=1
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                active=con.execute("SELECT * FROM habits").fetchall()
                for row in active:
                    self._purge_con(con,row)
                con.execute("DELETE FROM receipts")
                con.execute("DELETE FROM evidence")
            return {"cleared":len(active),"linked_memories_forgotten":linked_count}
        finally:
            with self._connect() as con:
                con.execute("UPDATE control SET clearing=NULL WHERE id=1 AND clearing=?",(token,))
