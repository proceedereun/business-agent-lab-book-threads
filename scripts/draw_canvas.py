#!/usr/bin/env python3
"""책 노트를 읽어 옆에 같은 이름의 캔버스를 그리는 스크립트 (design.md 6-1절).

실행: python3 scripts/draw_canvas.py "책 제목"   # 책 하나
      python3 scripts/draw_canvas.py             # 20_reading/books/의 책 노트 전부
책 노트는 읽기만 하고, 쓰는 파일은 "책 제목.canvas" 하나다.
카드의 글은 책 노트 줄을 글자 그대로 옮긴다(앞 들여쓰기와 "- "만 뗌).
같은 책 노트면 몇 번을 돌려도 같은 파일이 나온다.
"""

import json
import math
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_vault import CLOSING_LINES, LINK_RE, VAULT, Note, book_chains, progress_lines  # noqa: E402

BOOKS = VAULT / "20_reading" / "books"
STOPPED_RE = re.compile(r"^- \d{4}-\d{2}-\d{2} 사슬 (\d+)(?!\d).*멈춘 왜 → \[\[([^\[\]|#]+)")
WENT_RE = re.compile(r"→ 사슬 (\d+)에서 감$")

CARD_W = 360
FILE_W, FILE_H = 400, 320
GAP = 40
PER_RING = 16
CHARS_PER_LINE = 20
LINE_H = 26
PARA_GAP = 16
PAD = 24

WENT = "5"      # 간 길
STOPPED = "1"   # 멈춘 왜


def card_height(lines):
    rows = sum(max(1, math.ceil(len(line) / CHARS_PER_LINE)) for line in lines)
    return PAD + rows * LINE_H + (len(lines) - 1) * PARA_GAP


def item_text(line):
    text = line.strip()
    return text[2:] if text.startswith("- ") else text


class Canvas:
    def __init__(self):
        self.groups = []
        self.nodes = []
        self.edges = []
        self.by_id = {}

    def text(self, nid, lines, x, y, color=None):
        node = {"id": nid, "type": "text", "text": "\n\n".join(lines),
                "x": round(x), "y": round(y), "width": CARD_W, "height": card_height(lines)}
        if color:
            node["color"] = color
        self.nodes.append(node)
        self.by_id[nid] = node
        return node

    def file(self, nid, path, x, y):
        node = {"id": nid, "type": "file", "file": path,
                "x": round(x), "y": round(y), "width": FILE_W, "height": FILE_H}
        self.nodes.append(node)
        self.by_id[nid] = node
        return node

    def edge(self, a, b, color=None):
        e = {"id": f"e-{a}-{b}", "fromNode": a, "fromSide": "right", "toNode": b, "toSide": "left"}
        if color:
            e["color"] = color
        self.edges.append(e)

    def dump(self):
        return json.dumps({"nodes": self.groups + self.nodes, "edges": self.edges},
                          ensure_ascii=False, indent="\t") + "\n"


def vault_note_path(name):
    """[[이름]]이 가리키는 노트의 볼트 안 경로. 없으면 None."""
    hits = sorted(p for p in VAULT.rglob(f"{name}.md") if ".obsidian" not in p.parts)
    return hits[0].relative_to(VAULT).as_posix() if hits else None


def direction_lines(note, mine):
    sec = note.section(3, "방향 질문", mine)
    if not sec:
        return []
    return [item_text(note.lines[i]) for i in range(sec[0] + 1, sec[1]) if note.lines[i].strip()]


def underline_cards(note, mine):
    """### 밑줄과 생각의 항목마다 [줄, …]. 항목은 들여쓰기 없는 "- " 줄에서 시작한다."""
    sec = note.section(3, "밑줄과 생각", mine)
    if not sec:
        return []
    cards = []
    for i in range(sec[0] + 1, sec[1]):
        line = note.lines[i]
        if not line.strip():
            continue
        if line.startswith("- "):
            cards.append([item_text(line)])
        elif cards:
            cards[-1].append(item_text(line))
    return cards


def chain_cards(note, chain):
    """사슬 하나를 카드 나무로 바꾼다. 카드: (번호, 줄들, 깊이, 부모 카드, 종류)."""
    items = chain.items
    cards, meta = [], []
    card_of = {}   # 사슬 항목 번호 → 카드 번호
    for k, (_, _, kind, _, parent) in enumerate(items):
        raw = item_text(note.lines[items[k][0]])
        if kind == "나" and parent is None and not cards:
            cards.append([raw])
            card_of[k] = 0
            meta.append((0, None, "root"))
        elif kind == "Claude":
            nxt = k + 1
            answered = nxt < len(items) and items[nxt][2] == "나" and items[nxt][4] == k
            lines = [raw, item_text(note.lines[items[nxt][0]])] if answered else [raw]
            up = card_of.get(parent)
            cards.append(lines)
            card_of[k] = len(cards) - 1
            if answered:
                card_of[nxt] = len(cards) - 1
            meta.append((meta[up][0] + 1 if up is not None else 1, up, "went" if answered else "stopped"))
        elif kind == "안 간 가지":
            up = card_of.get(parent)
            cards.append([raw])
            meta.append((meta[up][0] + 1 if up is not None else 1, up, "branch"))
    if not cards:
        return [], []
    closing = []
    for i in range(chain.title_idx + 1, len(note.lines)):
        if note.lines[i].startswith("#"):
            break
        if note.lines[i].startswith(CLOSING_LINES):
            closing.append(note.lines[i].strip())
    return [(n, lines) + meta[n] for n, lines in enumerate(cards)], closing


def layout_order(cards):
    """위에서 아래로 놓는 순서. 카드 바로 아래에 그 카드의 안 간 가지, 그 아래에 간 길의 다음 카드."""
    kids = {}
    for card in cards:
        kids.setdefault(card[3], []).append(card)
    order = []

    def place(card):
        order.append(card)
        below = kids.get(card[0], [])
        order.extend(c for c in below if c[4] == "branch")
        for c in below:
            if c[4] != "branch":
                place(c)

    for card in kids.get(None, []):
        place(card)
    return order


def draw(book_path):
    note = Note(book_path.name, book_path.read_text(encoding="utf-8"))
    mine = note.section(2, "1. 내 구역")
    cv = Canvas()

    # 가운데: 방향 질문
    dlines = direction_lines(note, mine) if mine else []
    if dlines:
        h = card_height(dlines)
        cv.text("dir", dlines, -CARD_W / 2, -h / 2)

    # 둘레: 밑줄과 생각 (한 바퀴 16장, 넘치면 바깥 원)
    ucards = underline_cards(note, mine) if mine else []
    hmax = max([card_height(c) for c in ucards] + [card_height(dlines) if dlines else 0, 1])
    diag = math.hypot(CARD_W, hmax) + GAP
    r = max(diag / (2 * math.sin(math.pi / PER_RING)), diag)
    outer = max(CARD_W, hmax) / 2
    for start in range(0, len(ucards), PER_RING):
        ring = ucards[start:start + PER_RING]
        for j, lines in enumerate(ring):
            t = -math.pi / 2 + 2 * math.pi * j / len(ring)
            cx, cy = r * math.cos(t), r * math.sin(t)
            cv.text(f"u{start + j + 1:02d}", lines, cx - CARD_W / 2, cy - card_height(lines) / 2)
        outer = r + max(CARD_W, hmax) / 2
        r += max(CARD_W, hmax) + GAP * 2

    # 오른쪽: 사슬마다 나무 하나
    stopped_note = {}
    for _, line in progress_lines(note):
        m = STOPPED_RE.match(line)
        if m:
            stopped_note[int(m.group(1))] = m.group(2).strip()
    used_files = set()
    x0 = outer + 400
    top = -outer
    roots, went_from = {}, {}
    for chain in book_chains(note):
        cards, closing = chain_cards(note, chain)
        if not cards:
            continue
        n = chain.number if chain.number is not None else len(roots) + 1
        y = top
        right = x0 + CARD_W
        stopped_ids = []
        for num, lines, depth, up, kind in layout_order(cards):
            nid = f"c{n}-{num:02d}"
            x = x0 + depth * (CARD_W + 80)
            color = {"root": WENT, "went": WENT, "stopped": STOPPED}.get(kind)
            node = cv.text(nid, lines, x, y, color)
            y += node["height"] + GAP
            right = max(right, x + CARD_W)
            if up is not None:
                cv.edge(f"c{n}-{up:02d}", nid, color if kind != "branch" else None)
            if kind == "stopped":
                stopped_ids.append(nid)
            if kind == "branch":
                m = WENT_RE.search(lines[0])
                if m:
                    went_from[int(m.group(1))] = nid
        roots[n] = (f"c{n}-00", cards[0][1][0])
        if closing:
            node = cv.text(f"c{n}-end", closing, x0, y)
            y += node["height"] + GAP
        name = stopped_note.get(n)
        path = vault_note_path(name) if name else None
        if path and stopped_ids:
            last = cv.by_id[stopped_ids[-1]]
            fid = f"q{n}"
            cv.file(fid, path, last["x"] + CARD_W + 80, last["y"])
            cv.edge(stopped_ids[-1], fid, STOPPED)
            right = max(right, last["x"] + CARD_W + 80 + FILE_W)
            used_files.add(path)
        cv.groups.append({"id": f"c{n}-g", "type": "group", "label": chain.title,
                          "x": round(x0 - GAP), "y": round(top - GAP * 2),
                          "width": round(right - x0 + GAP * 2), "height": round(y - top + GAP * 2)})
        x0 = right + 200

    # 사슬의 출발점: 밑줄 카드, 또는 다른 사슬의 안 간 가지
    for chain in book_chains(note):
        n = chain.number
        if n not in roots:
            continue
        root_id, root_line = roots[n]
        if chain.from_chain is not None:
            if n in went_from:
                cv.edge(went_from[n], root_id, WENT)
            continue
        for k, lines in enumerate(ucards):
            if root_line.strip() in (line.strip() for line in lines[1:]):
                cv.edge(f"u{k + 1:02d}", root_id, WENT)
                break

    # 왼쪽: ## 5. 링크 (선 없음, 멈춘 왜 옆에 둔 노트는 다시 넣지 않음)
    links = []
    sec = note.section(2, "5. 링크")
    if sec:
        for i in range(sec[0] + 1, sec[1]):
            for m in LINK_RE.finditer(note.lines[i]):
                path = vault_note_path(m.group(1).split("|")[0].split("#")[0].strip())
                if path and path not in used_files and path not in links:
                    links.append(path)
    y = -(len(links) * (FILE_H + GAP) - GAP) / 2
    for k, path in enumerate(links):
        cv.file(f"l{k + 1:02d}", path, -outer - 400 - FILE_W, y)
        y += FILE_H + GAP

    out = book_path.with_suffix(".canvas")
    out.write_text(cv.dump(), encoding="utf-8", newline="\n")
    return out, len(cv.groups) + len(cv.nodes), len(cv.edges)


def main(argv):
    if argv:
        paths = []
        for title in argv:
            p = BOOKS / (title if title.endswith(".md") else f"{title}.md")
            if not p.is_file():
                print(f"책 노트가 없습니다: {p.relative_to(VAULT.parent).as_posix()}")
                return 1
            paths.append(p)
    else:
        paths = sorted(BOOKS.glob("*.md"))
    for p in paths:
        if Note(p.name, p.read_text(encoding="utf-8")).type != "book":
            continue
        out, nodes, edges = draw(p)
        print(f"그림: {out.relative_to(VAULT.parent).as_posix()} (카드 {nodes}, 선 {edges})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
