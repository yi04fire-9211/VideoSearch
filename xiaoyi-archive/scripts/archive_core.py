"""Read-only archive search with a browser and Skill-friendly JSON response."""
from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

INDEX = Path(os.environ.get("ARCHIVE_INDEX", "/tmp/xiaoyi-search.sqlite3"))
ORIGIN = os.environ.get("ARCHIVE_ORIGIN", "https://tcg-yi.com")

SONG_LYRICS = {
    "敕勒歌": ("心随天地走", "大漠的孤烟", "与月亮聊天", "走马敕勒川",
             "天似穹庐", "天苍苍野茫茫", "风吹草低"),
    "突然想爱你": ("在这昏暗的夜里", "专注的背影", "在这拥挤的人",
              "哼着你心爱的歌曲", "占领我的心", "突然想爱你"),
}
KNOWN_SONGS = set(SONG_LYRICS) | {"Love UU", "十点半的地铁", "如果我们不曾相遇", "爱的就是你"}
CONTEXT_ALIASES = (
    ("回家", "回老家", "回东北"),
    ("姥姥", "外婆"),
)


def compact(text):
    return "".join(char for char in text.casefold() if char.isalnum())


def song_query(query):
    quoted = re.search(r"《([^》]+)》", query)
    if quoted:
        return quoted[1].strip()
    simplified = compact(query)
    return next((song for song in sorted(KNOWN_SONGS, key=len, reverse=True)
                 if compact(song) in simplified), None)


def requested_date(query):
    match = re.search(r"(?:(20\d{2})\s*[年/.-]\s*)?(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日|号)?\s*(?:以来|之后|以后|起|到现在|至今)", query)
    if match:
        year, month, day = match.groups()
        return date(int(year) if year else date.today().year, int(month), int(day)).isoformat()


def quote_candidates(db, query, start_date, limit):
    normalized = compact(query)
    if len(normalized) < 10:
        return []
    size = 4 if len(normalized) < 20 else 5
    anchors = list(dict.fromkeys(normalized[i:i + size] for i in range(0, len(normalized) - size + 1, size)))
    expression = " OR ".join(f'"{anchor}"' for anchor in anchors)
    seeds = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title
        FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
        WHERE words MATCH ? AND v.recording_date>=coalesce(?, '')
        ORDER BY bm25(words) LIMIT 300
    """, (expression, start_date))
    hits = {}
    for day, bvid, part, start, title in seeds:
        nearby = db.execute("""
            SELECT start, text FROM cues WHERE bvid=? AND part=? AND start BETWEEN ? AND ?
            ORDER BY start, rowid
        """, (bvid, part, start - 12, start + 12)).fetchall()
        for index, (position, first) in enumerate(nearby):
            window = [text for when, text in nearby[index:] if when <= position + 12]
            joined = compact("".join(window))
            exact = normalized in joined and normalized not in compact("".join(window[1:]))
            cursor = 0
            count = 0
            for anchor in anchors:
                found = joined.find(anchor, cursor)
                if found >= 0:
                    cursor = found + len(anchor)
                    count += 1
            if exact or (anchors[0] in compact(first) and anchors[-1] in joined and count >= max(3, len(anchors) - 1)):
                key = bvid, part, int(position)
                row = (day, bvid, part, position, title, " ".join(window))
                hits[key] = (exact, count, row)
    return [result(row, "整句字幕命中" if exact else "连贯字幕候选")
            for exact, _, row in sorted(hits.values(), reverse=True)[:limit]]


def context_terms(query):
    cleaned = re.sub(r"^(?:帮我)?(?:找一下|找找|搜一下|搜索)", "", query).strip()
    parts = re.split(r"[\s、，,]+", cleaned)
    if len(parts) == 1:
        joined = re.fullmatch(r"(回家|回老家|回东北)(?:去?看(?:望)?)?(姥姥|外婆)", cleaned)
        parts = list(joined.groups()) if joined else parts
    if not 2 <= len(parts) <= 4 or not all(re.fullmatch(r"[\u3400-\u9fff]{2,6}", p) for p in parts):
        return []
    concepts = [next((aliases for aliases in CONTEXT_ALIASES if part in aliases), (part,))
                for part in parts]
    return list(dict.fromkeys(concepts))


def context_candidates(db, concepts, start_date, limit):
    # Use the trigram index when all variants of an anchor are indexable.
    anchor = next((terms for terms in reversed(concepts) if min(map(len, terms)) >= 3), concepts[-1])
    indexed = min(map(len, anchor)) >= 3
    if indexed:
        source = "words JOIN cues c ON c.rowid=words.rowid"
        predicate = "words MATCH ?"
        params = (" OR ".join(f'"{term}"' for term in anchor),)
    else:
        source = "cues c"
        predicate = "(" + " OR ".join("instr(c.text, ?) > 0" for _ in anchor) + ")"
        params = anchor
    seeds = db.execute(f"""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM {source} JOIN videos v USING(bvid)
        WHERE {predicate} AND instr(c.text, '♪')=0
          AND v.recording_date>=coalesce(?, '')
        LIMIT 2000
    """, (*params, start_date))
    hits = []
    window = 60
    for day, bvid, part, start, title, text in seeds:
        nearby = db.execute("""
            SELECT start, text FROM cues
            WHERE bvid=? AND part=? AND start BETWEEN ? AND ? AND instr(text,'♪')=0
            ORDER BY start, rowid
        """, (bvid, part, start - window, start + window)).fetchall()
        matched = [(start, text)]
        for terms in concepts:
            if terms == anchor:
                continue
            nearby_matches = [(position, line) for position, line in nearby
                              if any(term in line for term in terms)]
            if not nearby_matches:
                break
            matched.append(min(nearby_matches, key=lambda pair: abs(pair[0] - start)))
        if len(matched) != len(concepts):
            continue
        first, last = min(matched)[0], max(matched)[0]
        if last - first <= window:
            excerpt = " … ".join(dict.fromkeys(line for _, line in sorted(matched)))
            rank = last - first
            if all(aliases in concepts for aliases in CONTEXT_ALIASES):
                if re.search(r"看(?:见|望|看)?(?:我|咱)?(?:的)?(?:姥姥|外婆)", excerpt):
                    rank -= 30
                elif re.search(r"(?:我|咱)(?:的)?(?:姥姥|外婆)", excerpt):
                    rank -= 12
            hits.append((rank, (day, bvid, part, first, title, excerpt)))
    best = []
    positions = {}
    for _, row in sorted(hits, key=lambda hit: hit[0]):
        key = row[1:3]
        if any(abs(row[3] - previous) < window for previous in positions.get(key, [])):
            continue
        positions.setdefault(key, []).append(row[3])
        best.append(result(row, "上下文字幕候选"))
    return best[:limit]

def result(row, status):
    day, bvid, part, start, title, text = row
    seconds = int(start)
    return dict(date=day, bvid=bvid, part=part, seconds=seconds, title=title,
                excerpt=text, status=status,
                url=f"https://www.bilibili.com/video/{bvid}/?p={part}&t={seconds}")

def song_candidates(db, markers, start_date, limit):
    rows = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
        WHERE words MATCH ? AND v.recording_date>=coalesce(?, '')
        ORDER BY c.bvid, c.part, c.start
    """, (" OR ".join(f'"{marker}"' for marker in markers), start_date))
    spans = []
    for row in rows:
        if "♪" not in row[5]:
            continue
        if not spans or row[1:3] != spans[-1][-1][1:3] or row[3] - spans[-1][-1][3] > 45:
            spans.append([])
        spans[-1].append(row)
    matches = []
    for span in spans:
        distinct = {marker for row in span for marker in markers if marker in row[5]}
        if len(span) >= 2 and len(distinct) >= 2:
            matches.append(span[0])
    return [
        result(row, "歌词候选")
        for row in sorted(matches, key=lambda row: (row[0] or "", row[1], row[2], row[3]), reverse=True)[:limit]
    ]


def search(query, start_date=None, limit=20):
    start_date = start_date or requested_date(query)
    person_query = re.sub(r"(?<![a-z0-9])t\s*[.．]\s*燚|四火|小燚", "", query.lower())
    song = song_query(query)
    quotation = re.search(r'[「“"]([^」”"]+)[」”"]', query)
    speech = quotation[1] if quotation else re.sub(r"^(?:帮我)?(?:找一下|找找|搜一下|搜索)", "", query).strip()
    terms = re.findall(r"[\u3400-\u9fff]{3,}|[a-zA-Z]{2,}", speech)
    with closing(sqlite3.connect(INDEX.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if song or re.search(r"唱(?:过|了)?几次|什么时候唱过", query):
            return song_candidates(db, SONG_LYRICS[song], start_date, limit) if song in SONG_LYRICS else []
        for person, alias in db.execute("SELECT person, term FROM person_terms ORDER BY length(term) DESC"):
            if alias != person:
                person_query = person_query.replace(alias.lower(), person.lower())
        interaction = re.search(r"知名度|连麦|连线|\bpk\b", person_query)
        two_people = bool(re.search(r"单独|双人|两人|两个人", person_query))
        evidence = db.execute("""
            SELECT v.recording_date, e.bvid, e.part, e.start_seconds, v.title, e.note,
                   e.evidence_status
            FROM evidence e JOIN videos v USING(bvid)
            WHERE e.person != '' AND instr(?,lower(e.person))>0
              AND (
                e.activity=?
                OR trim(replace(?, lower(e.person), '')) IN ('', 't.')
                OR (
                  e.activity LIKE '%连麦'
                  AND ? IN ('连麦', '连线')
                )
              )
              AND (NOT ? OR e.activity='双人连麦')
              AND v.recording_date>=coalesce(?, '')
            ORDER BY e.evidence_status='字幕线索', v.recording_date DESC LIMIT ?
        """, (person_query, interaction[0] if interaction else None, person_query,
              interaction[0] if interaction else None, two_people, start_date, limit)).fetchall()
        if evidence:
            return [
                result(row[:-1], "字幕候选" if row[-1] == "字幕线索" else "已有核对记录")
                for row in evidence
            ]
        if interaction or two_people:
            return []
        concepts = context_terms(query) if not quotation else []
        if len(concepts) >= 2:
            return context_candidates(db, concepts, start_date, limit)
        if len(compact(speech)) >= 10:
            return quote_candidates(db, speech, start_date, limit)
        hits = {}
        for term in terms:
            if len(term) < 3:
                continue
            phrases = [term] if len(term) < 10 else [term[i:i+5] for i in range(0,len(term)-2,4)]
            for phrase in phrases[:12]:
                for day, bvid, part, start, title, text in db.execute("""
                    SELECT v.recording_date,c.bvid,c.part,c.start,v.title,c.text
                    FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
                    WHERE words MATCH ? AND v.recording_date>=coalesce(?, '')
                    ORDER BY bm25(words) LIMIT 150
                """, ('"' + phrase + '"', start_date)):
                    key = bvid, part, int(start) // 30
                    score, previous = hits.get(key, (0, None))
                    row = day,bvid,part,start,title,text
                    hits[key] = score + len(phrase), min(previous,row) if previous else row
    return [result(row, "字幕候选") for _,row in sorted(hits.values(),reverse=True)[:limit]]

# Pairs verified from matching lyrics and surrounding dialogue in the acceptance search.
MATCHED_VERSIONS = (
    ("BV1EbE26yEwk", "BV14EEU6FEty"),
    ("BV15aKa6AEub", "BV1H5Ka6yEzG", "BV1RQKY6MESu", "BV1ebg16jEvu"),
    ("BV1mVMz6LEGy", "BV1nKMB6wEoN", "BV1v7MB69EAi"),
    ("BV1efuH61EuU", "BV1oguH67EDu"),
    ("BV11UgV6VEaM", "BV1bxgj6dEmf"),
    ("BV1pV8U6XEUs", "BV1Y98m6iEiS"),
    ("BV14NtV6sEoi", "BV1jSt56VEmA"),
    ("BV115Jr6kE3o", "BV1Lap267E3Y"),
)
VERSION_KEYS = {bvid: group[0] for group in MATCHED_VERSIONS for bvid in group}


def group_results(results):
    groups = {}
    with closing(sqlite3.connect(INDEX.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        for item in results:
            bvid = item["bvid"]
            session = db.execute("SELECT session_id FROM videos WHERE bvid=?", (bvid,)).fetchone()
            key = VERSION_KEYS.get(bvid)
            if not key:
                key = bvid if item["status"] == "歌词候选" else ((session[0] if session else None) or bvid)
            versions = groups.setdefault((key, item["status"]), {})
            versions.setdefault(bvid, []).append(item)
        output = []
        for (key, status), versions in groups.items():
            if status == "歌词候选" and any(len(items) > 1 for items in versions.values()):
                output.extend([item] for items in versions.values() for item in items)
                continue
            row = db.execute("SELECT recording_date FROM videos WHERE bvid=?", (key,)).fetchone()
            items = sorted((items[0] for items in versions.values()), key=lambda item: item["bvid"] != key)
            if row and row[0] and key in VERSION_KEYS:
                items[0] = {**items[0], "date":row[0]}
            output.append(items)
    return output


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path != "/api/archive/search":
            return self.respond(404, {"error":"接口不存在"})
        params = parse_qs(path.query)
        query = params.get("q", [""])[0].strip()
        date = params.get("from", [None])[0]
        if not 1 <= len(query) <= 160 or (date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}",date)):
            return self.respond(400, {"error":"请提供搜索词和有效日期"})
        if not INDEX.is_file():
            return self.respond(503, {"error":"搜索索引暂不可用"})
        try:
            start_date = date or requested_date(query)
        except ValueError:
            return self.respond(400, {"error": "请提供有效日期"})
        results = search(query, start_date, limit=80)
        groups = group_results(results)
        payload = {"groups": groups[:20]}
        if song_query(query) or re.search(r"唱(?:过|了)?几次|什么时候唱过", query):
            scope = f"（{start_date} 起）" if start_date else ""
            payload["summary"] = (
                f"找到歌词候选{scope}，演唱者待核，暂无可报告的已核实次数。"
                if groups else f"当前档案暂无可确认的演唱证据{scope}。"
            )
            if len(groups) > 20:
                payload["summary"] += "当前显示前 20 组。"
        return self.respond(200, payload)

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","9000"))), Handler).serve_forever()
