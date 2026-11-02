"""Read-only Git-адаптер (спека, §13.2): проверяет коммит в заранее настроенном репозитории.

Только чтение: rev-parse, cat-file, rev-parse --is-bare/--git-dir. Команды передаются списком,
без shell. Произвольные URL не открываются: путь задаёт администратор проекта.
"""
import re
import subprocess
from pathlib import Path

HEX = re.compile(r"^[0-9a-f]{7,40}$")
TIMEOUT = 15


class GitError(Exception):
    pass


def _run(repo: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git недоступен: {exc.__class__.__name__}") from exc


def validate_repo(path: str) -> dict:
    """Проверяет, что путь — существующий git-репозиторий. Возвращает абсолютный путь и HEAD."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise GitError("путь не существует или не каталог")
    absolute = str(p.resolve())
    inside = _run(absolute, "rev-parse", "--git-dir")
    if inside.returncode != 0:
        raise GitError("путь не является git-репозиторием")
    head = _run(absolute, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return {"path": absolute, "head": head.stdout.strip() if head.returncode == 0 else None}


def resolve_commit(repo: str, sha: str) -> str | None:
    """Полный OID коммита или None, если коммита нет или SHA некорректен."""
    if not HEX.match(sha or ""):
        return None
    result = _run(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def prefix_matches(repo: str, sha: str) -> int:
    """Сколько объектов репозитория начинается с этого префикса (git требует от 4 символов)."""
    if not re.match(r"^[0-9a-f]{4,40}$", sha or ""):
        return 0
    result = _run(repo, "rev-parse", f"--disambiguate={sha}")
    return len(result.stdout.split()) if result.returncode == 0 else 0


def path_exists_at(repo: str, oid: str, path: str) -> bool:
    """Проверка code_refs из manifest: путь существует в закреплённом коммите."""
    return _run(repo, "cat-file", "-e", f"{oid}:{path}").returncode == 0


SEP = "\x1f"


def _safe_path(path: str) -> bool:
    return bool(path) and not path.startswith(("-", "/")) and ".." not in Path(path).parts


def file_history(repo: str, paths: list[str], pickaxe: str | None = None, limit: int = 8) -> dict:
    """Где добавлен и менялся тест: коммиты, затрагивавшие файлы теста.

    pickaxe — имя тестовой функции: тогда остаются только коммиты, где это имя добавлено или удалено в диффе.
    Возвращает {"added": коммит или None, "changes": [коммиты, новые первыми]}. Только чтение (git log)."""
    paths = [p for p in paths if _safe_path(p)]
    if not paths:
        return {"added": None, "changes": []}
    fmt = f"--format=%H{SEP}%s{SEP}%cI"
    extra = [f"-S{pickaxe}"] if pickaxe and re.fullmatch(r"\w+", pickaxe) else []

    def log(*flags: str) -> list[dict]:
        out = _run(repo, "log", fmt, *flags, *extra, "--", *paths)
        rows = []
        for line in out.stdout.splitlines() if out.returncode == 0 else []:
            sha, title, date = (line.split(SEP) + ["", ""])[:3]
            rows.append({"sha": sha, "title": title, "date": date})
        return rows

    changes = log("-n", str(limit))
    added = log("--diff-filter=A")
    return {"added": added[-1] if added else (changes[-1] if changes else None), "changes": changes}


def log_page(repo: str, offset: int, limit: int) -> dict:
    """Коммиты текущей ветки (HEAD), новые первыми. Только чтение (rev-list, log)."""
    count = _run(repo, "rev-list", "--count", "HEAD")
    total = int(count.stdout.strip()) if count.returncode == 0 and count.stdout.strip().isdigit() else 0
    fmt = f"--format=%H{SEP}%an{SEP}%aI{SEP}%s{SEP}%P"
    out = _run(repo, "log", fmt, f"--skip={offset}", f"-n{limit}", "HEAD")
    rows = []
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        sha, author, date, title, parents = (line.split(SEP) + ["", "", "", ""])[:5]
        rows.append({"sha": sha, "author": author, "date": date, "title": title, "parents": parents.split()})
    return {"total": total, "items": rows}


def show_commit(repo: str, oid: str) -> dict | None:
    """Подробности коммита: автор, дата, сообщение, родители, теги, изменённые файлы со статистикой."""
    if not HEX.match(oid or ""):
        return None
    head = _run(repo, "show", "-s", f"--format=%H{SEP}%an{SEP}%ae{SEP}%aI{SEP}%cn{SEP}%cI{SEP}%P{SEP}%s{SEP}%b", oid)
    if head.returncode != 0:
        return None
    sha, an, ae, ad, cn, cd, parents, subject, body = (head.stdout.split(SEP) + [""] * 9)[:9]
    stats = _run(repo, "diff-tree", "--root", "-r", "-M", "--numstat", "--no-commit-id", oid).stdout.splitlines()
    status = _run(repo, "diff-tree", "--root", "-r", "-M", "--name-status", "--no-commit-id", oid).stdout.splitlines()
    kinds = {}
    for line in status:
        parts = line.split("\t")
        kinds[parts[-1]] = parts[0][:1]
    files = []
    for line in stats:
        added, deleted, *path = line.split("\t")
        name = path[-1] if path else ""
        files.append({"path": name, "added": None if added == "-" else int(added),
                      "deleted": None if deleted == "-" else int(deleted), "status": kinds.get(name, "M")})
    tags = _run(repo, "tag", "--points-at", oid).stdout.split()
    return {"sha": sha.strip(), "author": an, "email": ae, "authored_at": ad, "committer": cn, "committed_at": cd,
            "parents": parents.split(), "subject": subject, "body": body.strip(), "tags": tags, "files": files}


DIFF_LIMIT = 400_000
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")


def file_diff(repo: str, oid: str, path: str) -> dict | None:
    """Unified diff одного файла коммита, разобранный по блокам и строкам с номерами. Только чтение (git show)."""
    if not HEX.match(oid or "") or not _safe_path(path):
        return None
    out = _run(repo, "show", "--format=", "-M", "-U3", "--no-color", "--no-ext-diff", oid, "--", path)
    if out.returncode != 0:
        return None
    raw = out.stdout
    truncated = len(raw) > DIFF_LIMIT
    raw = raw[:DIFF_LIMIT]
    binary = "Binary files" in raw or "GIT binary patch" in raw
    hunks, current, old_no, new_no = [], None, 0, 0
    for line in raw.splitlines():
        m = HUNK.match(line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            current = {"header": line, "section": m.group(3), "lines": []}
            hunks.append(current)
            continue
        if current is None or line.startswith("\\"):
            continue
        kind, text = line[:1], line[1:]
        if kind == "+":
            current["lines"].append({"type": "add", "old": None, "new": new_no, "text": text})
            new_no += 1
        elif kind == "-":
            current["lines"].append({"type": "del", "old": old_no, "new": None, "text": text})
            old_no += 1
        else:
            current["lines"].append({"type": "ctx", "old": old_no, "new": new_no, "text": text})
            old_no += 1
            new_no += 1
    return {"path": path, "binary": binary, "truncated": truncated, "hunks": hunks}


def repo_info(repo: str) -> dict:
    """Сведения о репозитории: ветка, число коммитов, авторы, теги, ветки, удалённые адреса без учётных данных."""
    def lines(*args: str) -> list[str]:
        r = _run(repo, *args)
        return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []
    count = lines("rev-list", "--count", "HEAD")
    first = lines("log", "--reverse", "--format=%aI", "--max-parents=0", "HEAD")
    head = lines("log", "-1", f"--format=%H{SEP}%s{SEP}%an{SEP}%aI", "HEAD")
    h = (head[0].split(SEP) + ["", "", "", ""])[:4] if head else None
    remotes = []
    for ln in lines("remote", "-v"):
        name, url, *_ = ln.split()
        url = re.sub(r"//[^/@\s]+@", "//", url)  # учётные данные из адреса не показываем
        if (name, url) not in remotes:
            remotes.append((name, url))
    authors = []
    for ln in lines("shortlog", "-sn", "HEAD")[:8]:
        n, _, name = ln.strip().partition("\t")
        authors.append({"name": name, "commits": int(n) if n.isdigit() else 0})
    tree = lines("ls-tree", "-r", "--name-only", "HEAD")
    top: dict[str, int] = {}
    for f in tree:
        key = f.split("/")[0] if "/" in f else "(корень)"
        top[key] = top.get(key, 0) + 1
    ext: dict[str, int] = {}
    for f in tree:
        e = Path(f).suffix.lower() or "без расширения"
        ext[e] = ext.get(e, 0) + 1
    return {"branch": (lines("branch", "--show-current") or [None])[0], "commits": int(count[0]) if count and count[0].isdigit() else 0,
            "first_commit_at": first[0] if first else None,
            "head": {"sha": h[0], "title": h[1], "author": h[2], "date": h[3]} if h else None,
            "branches": [b.lstrip("* ").strip() for b in lines("branch", "--format=%(refname:short)")][:20],
            "tags": lines("tag", "--sort=-creatordate")[:10], "remotes": [{"name": n, "url": u} for n, u in remotes],
            "authors": authors, "files": len(tree),
            "top_dirs": sorted(({"path": k, "files": v} for k, v in top.items()), key=lambda x: -x["files"])[:8],
            "extensions": sorted(({"ext": k, "files": v} for k, v in ext.items()), key=lambda x: -x["files"])[:6]}


def resolve_tag(repo: str, tag: str) -> str | None:
    """OID коммита, на который указывает тег (аннотированный или лёгкий). None — такого тега нет."""
    if not re.fullmatch(r"[\w][\w./+-]{0,199}", tag or "") or ".." in tag:
        return None
    if _run(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}").returncode != 0:
        return None
    out = _run(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else None


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """Достижим ли коммит ancestor из descendant (или это один и тот же коммит)."""
    if not HEX.match(ancestor or "") or not HEX.match(descendant or ""):
        return False
    return _run(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0
