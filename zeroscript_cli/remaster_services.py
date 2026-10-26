# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared reliability primitives for the ZeroScript-Win remaster (stdlib only)."""
from __future__ import annotations
import hashlib, json, os, re, shutil, socket, subprocess, time
from pathlib import Path
from typing import Any, Iterable

_SECRET = re.compile(r"(?:api.?key|token|secret|password|authorization|cookie|credential|roblosecurity)", re.I)
_IGNORES = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}

def redact(value: Any, depth: int = 0) -> Any:
    """Bound nested event payloads and redact values by sensitive field name."""
    if depth > 8: return "[depth limited]"
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if _SECRET.search(str(k)) else redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [redact(x, depth + 1) for x in value[:200]]
    if isinstance(value, str): return value[:4000]
    if value is None or isinstance(value, (bool, int, float)): return value
    return str(value)[:4000]

class AgentFlightRecorder:
    """Append-only JSONL event log with redaction and one-generation rotation."""
    def __init__(self, path: str | Path, max_bytes: int = 8 * 1024 * 1024):
        self.path, self.max_bytes = Path(path), max(65536, int(max_bytes))
    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"time": time.time(), "event": str(event)[:120], "fields": redact(fields)}
        data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
        if self.path.exists() and self.path.stat().st_size + len(data) > self.max_bytes:
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            rotated.unlink(missing_ok=True)
            self.path.replace(rotated)
        with self.path.open("ab") as stream: stream.write(data)
        return row
    def read(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path.exists(): return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(limit, 5000)):]:
            try:
                row = json.loads(line)
                if isinstance(row, dict): out.append(row)
            except ValueError: pass
        return out

class ProjectIntelligenceIndex:
    """Incremental SHA-256 file index with lightweight symbol/import extraction."""
    def __init__(self, root: str | Path, index_path: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.index_path = Path(index_path) if index_path else self.root / ".zeroscript" / "project-index.json"
    def build(self, max_files: int = 20000, max_file_bytes: int = 2_000_000) -> dict[str, Any]:
        if not self.root.is_dir(): raise ValueError("Project root must be an existing directory.")
        try: old = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): old = {}
        previous, files, scanned, changed = old.get("files", {}), {}, 0, 0
        for current, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in _IGNORES and not d.startswith(".") and not (Path(current)/d).is_symlink())
            for name in sorted(names):
                if name.startswith(".") or len(files) >= max(1, min(max_files, 100000)): continue
                p = Path(current) / name
                if p.is_symlink(): continue
                try:
                    if p.stat().st_size > max_file_bytes: continue
                    raw, text = p.read_bytes(), p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError): continue
                rel, digest = p.relative_to(self.root).as_posix(), hashlib.sha256(raw).hexdigest()
                scanned += 1
                if previous.get(rel, {}).get("sha256") == digest:
                    files[rel] = previous[rel]; continue
                changed += 1
                symbols = sorted(set(re.findall(r"(?m)^\s*(?:async\s+)?(?:def|class|function|interface|type|struct|enum)\s+([A-Za-z_$][\w$]*)", text)))[:500]
                imports = sorted(set(re.findall(r"(?m)^\s*(?:import\s+.*?\s+from\s+|import\s+|from\s+)([.A-Za-z0-9_/@-]+)", text)))[:300]
                files[rel] = {"sha256": digest, "bytes": len(raw), "symbols": symbols, "imports": imports}
        result = {"schema_version": 1, "root": str(self.root), "updated_at": time.time(),
                  "files_scanned": scanned, "files_changed": changed, "files_removed": sorted(set(previous)-set(files)), "files": files}
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); os.replace(tmp, self.index_path)
        return {k:v for k,v in result.items() if k != "files"}
    def search(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        if not query.strip(): return []
        try: data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc: raise RuntimeError("Build the project index first.") from exc
        needle, out = query.casefold(), []
        for path, item in data.get("files", {}).items():
            symbols = [x for x in item.get("symbols", []) if needle in x.casefold()]
            imports = [x for x in item.get("imports", []) if needle in x.casefold()]
            if needle in path.casefold() or symbols or imports: out.append({"path":path,"symbols":symbols,"imports":imports})
        return out[:max(1,min(limit,200))]

class TaskVerificationGate:
    """Only explicit passing evidence can satisfy an acceptance criterion."""
    @staticmethod
    def evaluate(criteria: Iterable[str], evidence: Iterable[dict[str, Any]]) -> dict[str, Any]:
        entries = list(evidence); rows = []
        for criterion in criteria:
            matches = [e for e in entries if str(e.get("criterion","")).strip() == str(criterion).strip()]
            passed = any(e.get("passed") is True and e.get("source") and e.get("summary") for e in matches)
            rows.append({"criterion":criterion,"status":"verified" if passed else "unverified",
                         "evidence":[{"source":e.get("source"),"summary":e.get("summary")} for e in matches]})
        return {"status":"verified" if rows and all(r["status"]=="verified" for r in rows) else "unverified","criteria":rows}

class PermissionPolicy:
    """Conservative shared policy helper; not an OS sandbox."""
    READ_PREFIXES = ("get_","list_","read_","search_","find_","inspect_","describe_","status","show_","query_","capture_","screenshot")
    # These operations are hard-denied in every mode, including --yes/auto.\n    # Keep this list in parity with docs/permission-policy-cases.json and the\n    # browser Autopilot NEVER_AUTOMATE gate.\n    DANGEROUS = re.compile(r"(?:^|[_/.-])(?:delete|remove|destroy|purge|wipe|format|reset(?:_workspace)?|set_root|open_recent|move_path|git_reset|git_commit|git_push|git_stage_file|git_unstage_file|publish|deploy|release|send_email|send_message|purchase|checkout|payment|credential|secret_rotation|change_password|install|uninstall)(?:$|[_/.-])", re.I)
    APPROVAL_REQUIRED = re.compile(r"(?:^|[_/.-])(?:set_root|move_path|git_commit|git_push|git_stage_file|git_unstage_file)(?:$|[_/.-])", re.I)
    @classmethod
    def decide(cls, tool: str, mode: str = "default") -> dict[str, Any]:
        name = str(tool or "").rsplit("/",1)[-1]
        if cls.DANGEROUS.search(name): return {"allowed":False,"requires_approval":True,"reason":"Destructive or externally consequential operation."}
        if mode == "plan":
            allowed = name.startswith(cls.READ_PREFIXES) or name in {"workspace_info","workspace_tree","workspace_read_file","workspace_git_status","workspace_git_diff","workspace_git_log"}
            return {"allowed":allowed,"requires_approval":not allowed,"reason":"Plan mode is read-only."}
        if mode not in {"default","auto"}: return {"allowed":False,"requires_approval":True,"reason":"Unknown permission mode."}
        approval = (
            cls.APPROVAL_REQUIRED.search(name) is not None or
            name in {"workspace_run_command","workspace_run_task","workspace_delete_file"}
        )
        return {"allowed":True,"requires_approval":approval and mode!="auto","reason":"Consequential operation." if approval else "Routine operation."}
class WorktreeManager:
    """Manage isolated Git worktrees using subprocess argument arrays."""
    def __init__(self, repo: str | Path): self.repo = Path(repo).resolve()
    def create(self, name: str, base: str = "HEAD", directory: str | Path | None = None) -> dict[str,str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}",name): raise ValueError("Invalid worktree name.")
        if not (self.repo/".git").exists(): raise ValueError("Selected directory is not a Git working tree.")
        target = Path(directory).expanduser().resolve() if directory else self.repo.parent/(self.repo.name+"-worktree-"+name)
        if target == self.repo or self.repo in target.parents:
            raise ValueError("Worktree target must be outside the selected repository.")
        base_ref = str(base).strip()
        if not base_ref or len(base_ref) > 200 or base_ref.startswith("-") or any(ch.isspace() for ch in base_ref):
            raise ValueError("Base ref must be a non-empty ref of at most 200 characters without whitespace or a leading dash.")
        if target.exists(): raise FileExistsError(str(target))
        branch = "zeroscript/"+name
        exists = subprocess.run(["git","-C",str(self.repo),"show-ref","--verify","--quiet","refs/heads/"+branch],capture_output=True,timeout=15)
        args = ["git","-C",str(self.repo),"worktree","add",str(target),branch] if exists.returncode == 0 else ["git","-C",str(self.repo),"worktree","add","-b",branch,str(target),base_ref]
        result = subprocess.run(args,capture_output=True,text=True,timeout=60)
        if result.returncode: raise RuntimeError((result.stderr or result.stdout or "git worktree add failed").strip())
        return {"path":str(target),"branch":branch,"stdout":result.stdout.strip()}
    def list(self) -> list[dict[str,str]]:
        r = subprocess.run(["git","-C",str(self.repo),"worktree","list","--porcelain"],capture_output=True,text=True,timeout=15,check=True)
        rows=[]; cur={}
        for line in r.stdout.splitlines()+[""]:
            if not line:
                if cur: rows.append(cur); cur={}
            else:
                k,_,v=line.partition(" "); cur[k]=v
        return rows

class IntegrationHealth:
    @staticmethod
    def tcp(host: str, port: int, timeout: float = 1.5) -> dict[str,Any]:
        start=time.monotonic()
        try:
            with socket.create_connection((host,int(port)),timeout=max(.1,min(timeout,5))): ok,error=True,None
        except (OSError,ValueError) as exc: ok,error=False,str(exc)
        return {"ok":ok,"host":host,"port":int(port),"latency_ms":round((time.monotonic()-start)*1000,2),**({"error":error} if error else {})}
    @staticmethod
    def command(argv: list[str], timeout: float = 3.0) -> dict[str,Any]:
        if not argv or any(not isinstance(x,str) for x in argv): raise ValueError("Provide a command argument array.")
        try:
            r=subprocess.run(argv,capture_output=True,text=True,timeout=max(.1,min(timeout,10)),shell=False)
            return {"ok":r.returncode==0,"returncode":r.returncode,"stdout":r.stdout[-2000:],"stderr":r.stderr[-2000:]}
        except (OSError,subprocess.TimeoutExpired) as exc: return {"ok":False,"error":str(exc)}

class WorkspaceSnapshot:
    """Capture selected workspace-relative files as recoverable copies."""
    def __init__(self, root: str | Path): self.root=Path(root).resolve(); self.base=self.root/".zeroscript"/"snapshots"
    def capture(self, paths: Iterable[str]) -> dict[str,Any]:
        stamp=time.strftime("%Y%m%d-%H%M%S")+"-"+str(time.time_ns()%1000000); dest=self.base/stamp; rows=[]
        for raw in paths:
            rel=Path(raw)
            if rel.is_absolute() or ".." in rel.parts: raise ValueError("Snapshot paths must stay inside the workspace.")
            src=(self.root/rel).resolve()
            if src!=self.root and self.root not in src.parents: raise ValueError("Snapshot path escaped workspace.")
            if not src.is_file() or src.is_symlink(): continue
            out=dest/rel; out.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,out)
            rows.append({"path":rel.as_posix(),"sha256":hashlib.sha256(src.read_bytes()).hexdigest(),"bytes":src.stat().st_size})
        dest.mkdir(parents=True,exist_ok=True)
        (dest/"manifest.json").write_text(json.dumps({"root":str(self.root),"files":rows},indent=2)+"\n",encoding="utf-8")
        return {"snapshot":str(dest),"files":len(rows),"manifest":rows}


    def restore(self, snapshot: str | Path, overwrite: bool = False) -> dict[str, Any]:
        """Restore only manifest-listed files after validating paths and content hashes."""
        source = Path(snapshot).resolve()
        if source != self.base and self.base not in source.parents:
            raise ValueError("Snapshot must be inside the workspace snapshot directory.")
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        restored, skipped = [], []
        for item in manifest.get("files", []):
            rel = Path(str(item.get("path", "")))
            if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                raise ValueError("Snapshot manifest contains an unsafe path.")
            backup = (source / rel).resolve()
            target = (self.root / rel).resolve()
            if source not in backup.parents or (target != self.root and self.root not in target.parents):
                raise ValueError("Snapshot manifest path escaped its permitted directory.")
            if not backup.is_file() or backup.is_symlink():
                raise ValueError("Snapshot file is missing or unsafe: " + rel.as_posix())
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            if digest != item.get("sha256"):
                raise ValueError("Snapshot hash mismatch: " + rel.as_posix())
            if target.exists() and not overwrite:
                skipped.append(rel.as_posix())
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            restored.append(rel.as_posix())
        return {"restored": restored, "skipped_existing": skipped, "overwrite": bool(overwrite)}

class ModelProfiles:
    ALLOWED={"base_url","model","label","vision","context_tokens"}
    def __init__(self,path: str | Path): self.path=Path(path)
    def load(self) -> dict[str,dict[str,Any]]:
        try: data=json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError: return {}
        if not isinstance(data,dict): raise ValueError("Profiles must be an object.")
        return {str(k):{f:v for f,v in p.items() if f in self.ALLOWED} for k,p in data.items() if isinstance(p,dict)}
    def save(self,name: str,profile: dict[str,Any]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,50}",name): raise ValueError("Invalid profile name.")
        clean={k:v for k,v in profile.items() if k in self.ALLOWED}
        if not isinstance(clean.get("base_url"),str) or not clean["base_url"].startswith(("http://","https://")): raise ValueError("Profile requires HTTP(S) base_url.")
        if not isinstance(clean.get("model"),str) or not clean["model"].strip(): raise ValueError("Profile requires model.")
        if "vision" in clean and not isinstance(clean["vision"], bool): raise ValueError("Profile vision capability must be boolean.")
        if "context_tokens" in clean:
            if isinstance(clean["context_tokens"], bool) or not isinstance(clean["context_tokens"], int) or clean["context_tokens"] < 256:
                raise ValueError("Profile context_tokens must be an integer of at least 256.")
        data=self.load(); data[name]=clean; self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix(".tmp"); tmp.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8"); os.replace(tmp,self.path)

class ArtifactGallery:
    EXTENSIONS={".png",".jpg",".jpeg",".webp",".gif",".mp4",".webm",".wav",".ogg",".mp3",".glb",".fbx",".obj",".blend",".zip",".log",".json",".pdf"}
    @classmethod
    def scan(cls,root: str | Path,limit: int=500) -> list[dict[str,Any]]:
        base=Path(root).resolve(); rows=[]
        for current,dirs,names in os.walk(base):
            dirs[:]=[d for d in dirs if d not in _IGNORES and not d.startswith(".")]
            for name in sorted(names):
                p=Path(current)/name
                if p.is_symlink() or p.suffix.lower() not in cls.EXTENSIONS or len(rows)>=max(1,min(limit,5000)): continue
                try: raw=p.read_bytes()
                except OSError: continue
                rows.append({"path":p.relative_to(base).as_posix(),"type":p.suffix[1:].lower(),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"modified_at":p.stat().st_mtime})
        return rows

def validate_tool_arguments(schema: dict[str,Any], args: dict[str,Any]) -> list[str]:
    """Validate common JSON Schema constraints recursively, failing closed on malformed shapes."""
    if not isinstance(schema, dict):
        return ["Tool input schema is invalid."]
    if not isinstance(args, dict):
        return ["Arguments must be an object."]
    errors: list[str] = []

    def check(spec: dict[str,Any], value: Any, path: str) -> None:
        kind = spec.get("type")
        checks = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
            "null": lambda v: v is None,
        }
        if kind in checks and not checks[kind](value):
            errors.append(f"{path} must be {kind}.")
            return
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"{path} is not an allowed value.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"{path} is below minimum.")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"{path} exceeds maximum.")
        if isinstance(value, str):
            if "minLength" in spec and len(value) < spec["minLength"]:
                errors.append(f"{path} is too short.")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                errors.append(f"{path} is too long.")
            if "pattern" in spec:
                try:
                    if re.search(str(spec["pattern"]), value) is None:
                        errors.append(f"{path} does not match the required pattern.")
                except re.error:
                    errors.append(f"{path} has an invalid schema pattern.")
        if isinstance(value, list):
            if "minItems" in spec and len(value) < spec["minItems"]:
                errors.append(f"{path} has too few items.")
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                errors.append(f"{path} has too many items.")
            item_schema = spec.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value[:1000]):
                    check(item_schema, item, f"{path}[{index}]")
        if isinstance(value, dict):
            properties = spec.get("properties", {})
            if not isinstance(properties, dict):
                errors.append(f"{path} has an invalid properties schema.")
                return
            for required in spec.get("required", []):
                if required not in value:
                    errors.append((f"Missing required property: {required}" if path == "Arguments" else f"{path} missing required property: {required}."))
            if spec.get("additionalProperties") is False:
                errors.extend((f"Unknown property: {key}" if path == "Arguments" else f"{path} has unknown property: {key}.") for key in value if key not in properties)
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, dict):
                    check(child_schema, child, f"{path}.{key}")

    check(schema, args, "Arguments")
    return errors[:100]


def validate_tool_dispatch(name: str, args: dict[str, Any], catalogue: Iterable[dict[str, Any]],
                           mode: str = "default") -> list[str]:
    """Fail-closed validation shared by CLI and bridge dispatch boundaries."""
    if not isinstance(name, str) or not name.strip():
        return ["Tool name is required."]
    if not isinstance(args, dict):
        return ["Tool arguments must be an object."]
    tools = [item for item in catalogue if isinstance(item, dict) and item.get("name") == name]
    if len(tools) != 1:
        return ["Tool is missing from the current catalogue or is ambiguous."]
    tool = tools[0]
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return ["Tool has no valid input schema; refusing to dispatch unvalidated arguments."]
    decision = PermissionPolicy.decide(name, mode)
    if not decision.get("allowed", False):
        return [str(decision.get("reason") or "Tool call denied by permission policy.")]
    errors = validate_tool_arguments(schema, args)
    if errors:
        return errors
    return []