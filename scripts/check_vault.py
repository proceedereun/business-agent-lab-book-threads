#!/usr/bin/env python3
"""샘플 볼트 검사 스크립트 (design.md 11절 "검사 스크립트").

실행: python3 scripts/check_vault.py
통과하면 종료 코드 0, 하나라도 실패하면 1.
"""

import datetime
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "sample-vault"
INBOX = "00_inbox/받은편지함.md"

ALLOWED_DIRS = {"00_inbox", "10_core", "10_core/questions", "20_reading", "20_reading/books"}
COMMON_PROPS = ["type", "description", "created", "author"]
BOOK_PROPS = COMMON_PROPS + ["started", "read_through", "finished", "calendar_events"]
DATE_PROPS = ["created", "started", "read_through", "finished"]
AUTHORS = {"me", "ai", "source", "mixed"}
AUTHOR_OF_TYPE = {"inbox": "me", "book": "mixed", "question": "me"}
BOOK_H2 = ["0. 진행 상태", "1. 내 구역", "2. AI 요약", "3. AI가 본 것", "4. 내 검토", "5. 링크"]
QUESTION_H2 = ["질문", "답의 이력", "행동과 결과"]
FORBIDDEN_CHARS = set('?:*"<>|\\#^[]')
CLOSING_LINES = ("알게 된 나:", "방향 질문에 대한 지금의 답:")
# 안 간 가지 2개 상한(4.9판)은 이 날짜 이후에 시작한 사슬에만 적용
BRANCH_CAP_FROM = "2026-10-28"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADING_RE = re.compile(r"^(#{1,6}) (.*?)\s*$")
PROP_RE = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
ITEM_RE = re.compile(r"^\s*- (.*)$")
LINK_RE = re.compile(r"\[\[([^\[\]]*)\]\]")
QUOTED_LINK_RE = re.compile(r'^"\[\[[^\[\]"]+\]\]"$')
CHAIN_LINE_RE = re.compile(r"^(\s*)- (나|Claude|안 간 가지):(.*)$")
BRANCH_RE = re.compile(r'^"(.+)"(?:\s*→ 사슬 (\d+)에서 감)?$')
CHAIN_TITLE_RE = re.compile(r'^사슬 (\d+) — (?:p\.\d+(?:~\d+)?|사슬 (\d+)의 "(.+)"에서)$')
PROGRESS_CHAIN_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) 사슬 (\d+)(?!\d)")
QA_COUNT_RE = re.compile(r"문답 (\d+)회")


def rel(path):
    return path.relative_to(ROOT).as_posix()


def short(text, n=40):
    return text if len(text) <= n else text[:n] + "…"


class Result:
    def __init__(self, name):
        self.name = name
        self.failures = []
        self.notes = []
        self.skipped = None

    def fail(self, where, line, msg, section):
        loc = where if line is None else f"{where}:{line}"
        self.failures.append(f"{loc} — {msg} (design.md {section})")


class Note:
    """노트 한 장. 지금 파일이든 HEAD의 파일이든 글만 있으면 만든다."""

    def __init__(self, where, text):
        self.where = where
        self.lines = text.splitlines()
        self.props = {}
        self.prop_errors = []
        self.has_front_matter = False
        self.body_start = 0
        self._parse_front_matter()
        self.type = self.prop("type")
        self.headings = []
        for i in range(self.body_start, len(self.lines)):
            m = HEADING_RE.match(self.lines[i])
            if m:
                self.headings.append((i, len(m.group(1)), m.group(2)))

    def _parse_front_matter(self):
        if not self.lines or self.lines[0].strip() != "---":
            return
        for end in range(1, len(self.lines)):
            if self.lines[end].strip() == "---":
                break
        else:
            self.prop_errors.append((1, "속성 칸을 닫는 '---'가 없음"))
            return
        self.has_front_matter = True
        self.body_start = end + 1
        current = None
        for i in range(1, end):
            line = self.lines[i]
            m = PROP_RE.match(line)
            item = ITEM_RE.match(line)
            if m:
                current = {"value": m.group(2).strip(), "items": [], "line": i + 1}
                self.props[m.group(1)] = current
            elif item and current is not None:
                current["items"].append(item.group(1).strip())
            elif line.strip():
                self.prop_errors.append((i + 1, f"속성 줄의 모양을 읽을 수 없음: {short(line.strip())}"))

    def prop(self, key):
        p = self.props.get(key)
        return p["value"] if p else None

    def section(self, level, title, within=None):
        """제목 줄의 위치와, 그 아래 다음 같은(또는 더 높은) 제목 전까지의 줄 범위."""
        lo, hi = within if within else (0, len(self.lines))
        for k, (i, lv, text) in enumerate(self.headings):
            if lv == level and text == title and lo <= i < hi:
                end = len(self.lines)
                for j, lv2, _ in self.headings[k + 1:]:
                    if lv2 <= level:
                        end = j
                        break
                return i, min(end, hi)
        return None

    def headings_in(self, level, rng):
        return [(i, t) for i, lv, t in self.headings if lv == level and rng[0] <= i < rng[1]]


class Chain:
    def __init__(self, note, title_idx, title, end):
        self.title_idx = title_idx
        self.title = title
        m = CHAIN_TITLE_RE.match(title)
        self.title_ok = bool(m)
        self.number = int(m.group(1)) if m else None
        self.from_chain = int(m.group(2)) if m and m.group(2) else None
        self.items = []  # (줄 위치, 들여쓰기, 종류, 내용, 부모 항목 번호)
        for i in range(title_idx + 1, end):
            lm = CHAIN_LINE_RE.match(note.lines[i])
            if not lm:
                continue
            indent = len(lm.group(1).expandtabs(4))
            parent = None
            for k in range(len(self.items) - 1, -1, -1):
                if self.items[k][1] < indent:
                    parent = k
                    break
            self.items.append((i, indent, lm.group(2), lm.group(3).strip(), parent))


def book_chains(note):
    mine = note.section(2, "1. 내 구역")
    if not mine:
        return []
    chains = []
    h3 = note.headings_in(3, mine)
    for k, (i, title) in enumerate(h3):
        if title.startswith("사슬"):
            end = h3[k + 1][0] if k + 1 < len(h3) else mine[1]
            chains.append(Chain(note, i, title, end))
    return chains


def progress_lines(note):
    sec = note.section(2, "0. 진행 상태")
    if not sec:
        return []
    return [(i, note.lines[i]) for i in range(sec[0] + 1, sec[1])]


def chain_start_dates(note):
    dates = {}
    for _, line in progress_lines(note):
        m = PROGRESS_CHAIN_RE.match(line)
        if m:
            dates.setdefault(int(m.group(2)), m.group(1))
    return dates


# ---- git ----

def git(*args):
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=ROOT,
                             capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout


def head_files():
    if git("rev-parse", "--verify", "HEAD") is None:
        return None
    out = git("ls-tree", "-r", "-z", "--name-only", "HEAD", "--", "sample-vault")
    if out is None:
        return None
    return [p for p in out.decode("utf-8").split("\0") if p]


def head_text(path):
    out = git("show", f"HEAD:{path}")
    return None if out is None else out.decode("utf-8")


# ---- 검사 항목 ----

def vault_entries():
    for p in sorted(VAULT.rglob("*")):
        parts = p.relative_to(VAULT).parts
        if ".obsidian" in parts or p.name == ".gitkeep":
            continue
        yield p


def check_structure(notes):
    r = Result("구조")
    for p in vault_entries():
        relp = p.relative_to(VAULT).as_posix()
        if p.is_dir():
            if relp not in ALLOWED_DIRS:
                r.fail(rel(p) + "/", None, "11절에 없는 폴더", "11절")
            continue
        folder = p.parent.relative_to(VAULT).as_posix()
        if folder not in ALLOWED_DIRS and folder != ".":
            continue  # 폴더 자체가 이미 실패
        note = notes.get(p)
        if folder in (".", "10_core"):
            r.fail(rel(p), None, "이 자리에는 파일을 두지 않음", "11절")
        elif folder == "00_inbox":
            if p.name != "받은편지함.md":
                r.fail(rel(p), None, "00_inbox에는 받은편지함.md 한 파일만 둠", "4-3절, 11절")
            elif note and note.type and note.type != "inbox":
                r.fail(rel(p), None, f"type이 폴더와 맞지 않음 ({note.type}, 이 폴더는 inbox)", "11절")
        elif folder == "10_core/questions":
            if p.suffix != ".md":
                r.fail(rel(p), None, "질문 노트 폴더에는 .md 노트만 둠", "11절")
            elif note and note.type and note.type != "question":
                r.fail(rel(p), None, f"type이 폴더와 맞지 않음 ({note.type}, 이 폴더는 question)", "11절")
        elif folder == "20_reading":
            if p.name != "서재.base":
                r.fail(rel(p), None, "20_reading에는 서재.base만 둠", "6-1절, 11절")
        elif folder == "20_reading/books":
            if p.suffix not in (".md", ".canvas"):
                r.fail(rel(p), None, "책 폴더에는 책 노트(.md)와 캔버스(.canvas)만 둠", "6-1절, 11절")
            elif note and note.type and note.type != "book":
                r.fail(rel(p), None, f"type이 폴더와 맞지 않음 ({note.type}, 이 폴더는 book)", "11절")
    return r


def check_file_names():
    r = Result("파일 이름")
    for p in vault_entries():
        bad = sorted(set(p.name) & FORBIDDEN_CHARS)
        if bad:
            r.fail(rel(p), None, f"이름에 쓰지 않는 문자가 있음: {' '.join(bad)}", "11절")
    return r


def check_properties(notes):
    r = Result("속성")
    for p, note in notes.items():
        w = rel(p)
        for line, msg in note.prop_errors:
            r.fail(w, line, msg, "6절")
        if not note.has_front_matter:
            if not note.prop_errors:
                r.fail(w, 1, "속성 칸(---)이 없음", "6절, 11절")
            continue
        for key in COMMON_PROPS:
            if key not in note.props:
                r.fail(w, 1, f"공통 속성 '{key}'가 없음", "6절, 11절")
        t, a = note.type, note.prop("author")
        if t is not None and t not in AUTHOR_OF_TYPE:
            r.fail(w, note.props["type"]["line"], f"type '{t}'은 inbox·book·question 중 하나가 아님", "11절")
        if a is not None:
            if a not in AUTHORS:
                r.fail(w, note.props["author"]["line"], f"author '{a}'는 me·ai·source·mixed 중 하나가 아님", "11절")
            elif t in AUTHOR_OF_TYPE and a != AUTHOR_OF_TYPE[t]:
                r.fail(w, note.props["author"]["line"],
                       f"{t} 노트의 author는 {AUTHOR_OF_TYPE[t]}여야 함 (지금 {a})", "11절")
        if t == "book":
            for key, prop in note.props.items():
                if key not in BOOK_PROPS:
                    r.fail(w, prop["line"], f"책 노트에 6절에 없는 속성 '{key}'가 있음", "6절")
            for key in BOOK_PROPS[4:]:
                if key not in note.props:
                    r.fail(w, 1, f"독서 모듈 속성 '{key}'가 없음", "6절")
        if t == "question":
            src = note.props.get("source")
            if src is None:
                r.fail(w, 1, "질문 노트에 source가 없음", "7절")
            elif not src["value"] and not src["items"]:
                r.fail(w, src["line"], "질문 노트의 source가 비어 있음", "7절")
    return r


def link_values_ok(value):
    if value.startswith("[") and not value.startswith("[["):
        inner = value[1:-1] if value.endswith("]") else value[1:]
        return all(link_values_ok(v.strip()) for v in inner.split(",") if v.strip())
    return "[[" not in value or bool(QUOTED_LINK_RE.match(value))


def check_dates_and_formats(notes):
    r = Result("날짜·형식")
    for p, note in notes.items():
        w = rel(p)
        dates = {}
        for key in DATE_PROPS:
            prop = note.props.get(key)
            if prop is None:
                continue
            if prop["items"]:
                r.fail(w, prop["line"], f"{key}에 목록이 들어 있음 (날짜 하나)", "6절")
                continue
            v = prop["value"]
            if not v:
                if key == "created":
                    r.fail(w, prop["line"], "created가 비어 있음", "6절")
                continue
            try:
                if not DATE_RE.match(v):
                    raise ValueError
                dates[key] = datetime.date.fromisoformat(v)
            except ValueError:
                r.fail(w, prop["line"], f"{key} '{v}'가 YYYY-MM-DD 날짜가 아님", "6절")
        for a, b in (("started", "read_through"), ("read_through", "finished"), ("started", "finished")):
            if a in dates and b in dates and dates[a] > dates[b]:
                r.fail(w, note.props[b]["line"], f"{a}({dates[a]})가 {b}({dates[b]})보다 늦음", "4-1절")
        for key, prop in note.props.items():
            for v in [prop["value"]] + prop["items"]:
                if v and not link_values_ok(v):
                    r.fail(w, prop["line"], f'{key}의 링크는 따옴표 안에 "[[이름]]"으로 씀: {short(v)}', "6절")
        ce = note.props.get("calendar_events")
        if ce is not None and not ce["items"] and not ce["value"].startswith("["):
            r.fail(w, ce["line"], "calendar_events가 목록 모양([] 또는 - 값)이 아님", "6절")
    return r


def protected_lines(note):
    """HEAD에서 지켜야 할 내 말 줄: (줄 위치, 들여쓰기 뺀 글)."""
    if note.type == "question":
        return [(i, l.strip()) for i, l in enumerate(note.lines) if l.strip() and l.strip() != "---"]
    if note.type != "book":
        return []
    keep = set()
    for i in range(note.body_start, len(note.lines)):
        s = note.lines[i].strip()
        if s.startswith("- 나:"):
            keep.add(i)
        elif s.startswith(CLOSING_LINES) and s.split(":", 1)[1].strip():
            keep.add(i)
    mine = note.section(2, "1. 내 구역")
    for sec in (note.section(3, "본질 한 줄", within=mine) if mine else None,
                note.section(2, "4. 내 검토")):
        if sec:
            keep.update(i for i in range(sec[0] + 1, sec[1]) if note.lines[i].strip())
    return [(i, note.lines[i].strip()) for i in sorted(keep)]


def check_my_words(files_at_head):
    r = Result("내 말")
    if files_at_head is None:
        r.skipped = "git의 HEAD를 읽을 수 없음"
        return r
    for path in files_at_head:
        if not path.endswith(".md"):
            continue
        text = head_text(path)
        if text is None:
            continue
        old = Note(path, text)
        lines = protected_lines(old)
        if not lines:
            continue
        sec = "7절" if old.type == "question" else "3절, 6절"
        now = ROOT / path
        if not now.exists():
            r.fail(path, None, f"HEAD에 있던 노트가 지금 없음 (내 말 {len(lines)}줄)", f"{sec}, 11절")
            continue
        have = Counter(l.strip() for l in now.read_text(encoding="utf-8").splitlines())
        for i, s in lines:
            if have[s] > 0:
                have[s] -= 1
            else:
                r.fail(path, f"HEAD {i + 1}", f'HEAD에 있던 내 말 줄이 지금 파일에 글자 그대로 없음: "{short(s)}"', sec)
    return r


def check_inbox(files_at_head):
    r = Result("받은편지함")
    if files_at_head is None:
        r.skipped = "git의 HEAD를 읽을 수 없음"
        return r
    path = f"sample-vault/{INBOX}"
    if path not in files_at_head:
        r.notes.append("HEAD에 받은편지함이 없어 지워진 메모 줄 없음")
        return r
    old_lines = head_text(path).splitlines()
    now = ROOT / path
    have = Counter(l.strip() for l in now.read_text(encoding="utf-8").splitlines()) if now.exists() else Counter()
    removed = 0
    for i, line in enumerate(old_lines):
        s = line.strip()
        if not s.startswith("- ") or s.count("|") < 3:
            continue
        if have[s] > 0:
            have[s] -= 1
            continue
        removed += 1
        _, book, _, words = [part.strip() for part in s[2:].split("|", 3)]
        where = f"{path}:HEAD {i + 1}"
        if not book:
            r.fail(where, None, "어느 책인지 모르는 메모를 지움 (남겨 두고 물어야 함)", "4-3절")
            continue
        book_path = VAULT / "20_reading/books" / f"{book}.md"
        if not book_path.exists():
            r.fail(where, None, f"지운 메모의 책 노트가 없음: {book}", "4-3절")
            continue
        target = f"- 나: {words}"
        if not any(l.strip() == target for l in book_path.read_text(encoding="utf-8").splitlines()):
            r.fail(where, None, f'지운 메모의 내 말이 책 노트에 "나:" 줄로 글자 그대로 없음: "{short(words)}"', "4-3절")
    if removed == 0:
        r.notes.append("지워진 메모 줄 없음")
    return r


def check_branches(books):
    r = Result("안 간 가지")
    for p, note in books.items():
        w = rel(p)
        chains = book_chains(note)
        numbers = {c.number for c in chains if c.number}
        starts = chain_start_dates(note)
        for c in chains:
            capped = c.number is None or starts.get(c.number, BRANCH_CAP_FROM) >= BRANCH_CAP_FROM
            if not capped:
                r.notes.append(f"[건너뜀] 사슬 {c.number}: 4.9판 규칙 이전 기록 ({w})")
            per_parent = Counter()
            for i, _, kind, content, parent in c.items:
                if kind != "안 간 가지":
                    continue
                m = BRANCH_RE.match(content)
                if not m:
                    r.fail(w, i + 1, '안 간 가지는 "인용" 또는 "인용" → 사슬 n에서 감 모양으로 씀', "6절")
                    continue
                if parent is None or c.items[parent][2] != "나":
                    r.fail(w, i + 1, "안 간 가지가 나: 줄에 한 칸 들여 매달려 있지 않음", "5절 6번, 6절")
                    continue
                quote, goto = m.group(1), m.group(2)
                if quote not in c.items[parent][3]:
                    r.fail(w, i + 1, f'인용이 바로 위 나: 줄({c.items[parent][0] + 1}줄)에 글자 그대로 없음: "{short(quote)}"',
                           "5절 6번, 6절")
                if goto and int(goto) not in numbers:
                    r.fail(w, i + 1, f"'→ 사슬 {goto}에서 감'이 가리키는 사슬이 없음 (### 사슬 {goto})", "5절 6번")
                per_parent[parent] += 1
                if capped and per_parent[parent] == 3:
                    r.fail(w, i + 1, f"나: 줄({c.items[parent][0] + 1}줄) 하나에 안 간 가지가 2개를 넘음", "5절 6번")
    return r


def check_order(books):
    r = Result("순서")
    for p, note in books.items():
        w = rel(p)
        first = next((i for i, _, _ in note.headings), len(note.lines))
        for i in range(note.body_start, first):
            if note.lines[i].strip():
                r.fail(w, i + 1, "구역 제목보다 위에 글이 있음", "6절")
                break
        seen = []
        for i, lv, text in note.headings:
            if lv == 1:
                r.fail(w, i + 1, f"6절에 없는 제목: # {text}", "6절")
            elif lv == 2:
                if text not in BOOK_H2:
                    r.fail(w, i + 1, f"6절에 없는 구역 제목: ## {text}", "6절")
                elif text in [t for _, t in seen]:
                    r.fail(w, i + 1, f"구역 제목이 두 번 나옴: ## {text}", "6절")
                else:
                    if seen and BOOK_H2.index(text) < BOOK_H2.index(seen[-1][1]):
                        r.fail(w, i + 1, f"'## {text}' 제목이 '## {seen[-1][1]}'보다 아래에 있음 (AI 구역은 내 구역 아래)", "6절")
                    seen.append((i, text))
        for text in BOOK_H2:
            if text not in [t for _, t in seen]:
                r.fail(w, None, f"구역 제목이 없음: ## {text}", "6절")
        mine = note.section(2, "1. 내 구역")
        if not mine:
            continue
        rank_of = {"방향 질문": 0, "밑줄과 생각": 1, "본질 한 줄": 3}
        last = -1
        counts = Counter()
        for i, text in note.headings_in(3, mine):
            rank = 2 if text.startswith("사슬") else rank_of.get(text)
            if rank is None:
                r.fail(w, i + 1, f"내 구역에 6절에 없는 제목: ### {text}", "6절")
                continue
            counts[rank] += 1
            if rank < last:
                r.fail(w, i + 1, f"'### {text}' 제목의 자리가 6절 순서(방향 질문 → 밑줄과 생각 → 사슬 → 본질 한 줄)와 다름", "6절")
            last = max(last, rank)
        for text, rank in rank_of.items():
            if counts[rank] != 1:
                r.fail(w, mine[0] + 1, f"내 구역에 '### {text}' 제목이 한 번 있어야 함 (지금 {counts[rank]}번)", "6절")
    return r


def check_chain_titles(books):
    r = Result("사슬 제목")
    for p, note in books.items():
        w = rel(p)
        chains = book_chains(note)
        by_number = {c.number: c for c in chains if c.number}
        for expected, c in enumerate(chains, start=1):
            line = c.title_idx + 1
            if not c.title_ok:
                r.fail(w, line, f'사슬 제목은 "사슬 n — p.쪽수" 또는 "사슬 n — 사슬 m의 "인용"에서"로 씀: ### {c.title}', "6절")
                continue
            if c.number != expected:
                r.fail(w, line, f"사슬 번호가 이어지지 않음 (여기는 사슬 {expected}이어야 함)", "6절")
            m = c.from_chain
            if m is None:
                continue
            if m >= c.number or m not in by_number:
                r.fail(w, line, f"출발한 사슬이 앞에 없음 (### 사슬 {m})", "5절 6번, 6절")
                continue
            marked = any(kind == "안 간 가지" and content.endswith(f"→ 사슬 {c.number}에서 감")
                         for _, _, kind, content, _ in by_number[m].items)
            if not marked:
                r.fail(w, line, f"사슬 {m}에 '→ 사슬 {c.number}에서 감'이 붙은 안 간 가지가 없음", "5절 6번")
    return r


def check_question_notes(questions):
    r = Result("질문 노트")
    for p, note in questions.items():
        w = rel(p)
        h2 = [(i, t) for i, lv, t in note.headings if lv == 2]
        titles = [t for _, t in h2]
        for text in QUESTION_H2:
            if titles.count(text) != 1:
                r.fail(w, None, f"'## {text}' 제목이 한 번 있어야 함 (지금 {titles.count(text)}번)", "7절")
        for i, t in h2:
            if t not in QUESTION_H2:
                r.fail(w, i + 1, f"7절에 없는 제목: ## {t}", "7절")
        known = [t for t in titles if t in QUESTION_H2]
        if len(set(known)) == len(known) and known != [t for t in QUESTION_H2 if t in known]:
            r.fail(w, None, "제목 순서가 7절(질문 → 답의 이력 → 행동과 결과)과 다름", "7절")
        hist = note.section(2, "답의 이력")
        if hist:
            for i, t in note.headings_in(3, hist):
                if not DATE_RE.match(t):
                    r.fail(w, i + 1, f"답의 이력의 제목은 날짜(YYYY-MM-DD)로 씀: ### {t}", "7절")
    return r


def check_progress(books):
    r = Result("진행 상태")
    for p, note in books.items():
        w = rel(p)
        chains = {c.number: c for c in book_chains(note) if c.number}
        reported = {}
        for i, line in progress_lines(note):
            m, q = PROGRESS_CHAIN_RE.match(line), QA_COUNT_RE.search(line)
            if m and q:
                reported.setdefault(int(m.group(2)), []).append((i, int(q.group(1))))
        for n, entries in reported.items():
            if n not in chains:
                r.fail(w, entries[-1][0] + 1, f"진행 상태에 적힌 사슬이 내 구역에 없음 (### 사슬 {n})", "4절")
                continue
            actual = sum(1 for item in chains[n].items if item[2] == "Claude")
            *earlier, (last_i, last_k) = entries
            if last_k != actual:
                r.fail(w, last_i + 1, f"사슬 {n}의 문답 {last_k}회가 Claude: 줄 수({actual})와 다름", "4절")
            for i, k in earlier:
                if k > actual:
                    r.fail(w, i + 1, f"사슬 {n}의 문답 {k}회가 Claude: 줄 수({actual})보다 많음", "4절")
    return r


def check_links(notes):
    r = Result("링크")
    names = set()
    for p in vault_entries():
        if p.is_file():
            relp = p.relative_to(VAULT).as_posix()
            names.update({p.name, relp})
            if p.suffix == ".md":
                names.update({p.stem, relp[:-3]})
    for p, note in notes.items():
        for i, line in enumerate(note.lines):
            for m in LINK_RE.finditer(line):
                target = m.group(1).split("|")[0].split("#")[0].strip()
                if target and target not in names:
                    r.fail(rel(p), i + 1, f"가리키는 노트가 볼트 안에 없음: [[{m.group(1)}]]", "11절")
    return r


def check_canvases():
    r = Result("캔버스")
    canvases = [p for p in vault_entries() if p.suffix == ".canvas"]
    if not canvases:
        r.skipped = ".canvas 파일 없음"
        return r
    for p in canvases:
        w = rel(p)
        book = p.with_suffix(".md")
        if not book.exists():
            r.fail(w, None, f"같은 이름의 책 노트가 없음: {book.name}", "6-1절")
            continue
        text = book.read_text(encoding="utf-8")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            r.fail(w, None, "캔버스 파일을 JSON으로 읽을 수 없음", "6-1절")
            continue
        for node in data.get("nodes", []):
            if node.get("type") == "text":
                for line in node.get("text", "").splitlines():
                    if line.strip() and line.strip() not in text:
                        r.fail(w, None, f'카드 {node.get("id")}의 글이 책 노트에 글자 그대로 없음: "{short(line.strip())}"',
                               "6-1절 캔버스 규칙 1·5")
            elif node.get("type") == "file" and not (VAULT / node.get("file", "")).is_file():
                r.fail(w, None, f'카드 {node.get("id")}가 가리키는 파일이 볼트에 없음: {node.get("file")}', "6-1절")
    return r


def main():
    if not VAULT.is_dir():
        print(f"sample-vault/ 폴더가 없습니다: {VAULT}")
        return 1
    notes = {p: Note(rel(p), p.read_text(encoding="utf-8")) for p in vault_entries() if p.suffix == ".md"}
    books = {p: n for p, n in notes.items() if n.type == "book"}
    questions = {p: n for p, n in notes.items() if n.type == "question"}
    at_head = head_files()

    results = [
        check_structure(notes),
        check_file_names(),
        check_properties(notes),
        check_dates_and_formats(notes),
        check_my_words(at_head),
        check_inbox(at_head),
        check_branches(books),
        check_order(books),
        check_chain_titles(books),
        check_question_notes(questions),
        check_progress(books),
        check_links(notes),
        check_canvases(),
    ]

    print("검사 대상: sample-vault/\n")
    for res in results:
        if res.skipped:
            print(f"[건너뜀] {res.name} — {res.skipped}")
        elif res.failures:
            print(f"[실패 {len(res.failures)}건] {res.name}")
        else:
            print(f"[통과] {res.name}")
        for note in res.notes:
            print(f"    {note}")
        for f in res.failures:
            print(f"    {f}")
    failed = sum(1 for res in results if res.failures)
    skipped = sum(1 for res in results if res.skipped)
    print(f"\n결과: 통과 {len(results) - failed - skipped}, 실패 {failed}, 건너뜀 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
