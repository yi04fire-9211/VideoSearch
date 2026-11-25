"""Read-only archive search with a browser and Skill-friendly JSON response."""
from calendar import monthrange
from contextlib import closing
from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

INDEX = Path(os.environ.get("ARCHIVE_INDEX", "/tmp/xiaoyi-search.sqlite3"))
ORIGIN = os.environ.get("ARCHIVE_ORIGIN", "https://tcg-yi.com")
WEB_RESULT_LIMIT = 400

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
ASR_SPELLINGS = {"勾观": ("勾官",)}


@lru_cache(maxsize=1)
def people_index():
    path = Path(__file__).with_name("people_index.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@lru_cache(maxsize=1)
def character_pinyin():
    path = Path(__file__).with_name("character_pinyin.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def short_quote_candidates(db, speech, start_date, end_date, limit):
    query = compact(speech)
    sounds = character_pinyin()
    anchors = {query[i:i + 3] for i in range(len(query) - 2)}
    expression = " OR ".join(f'"{anchor}"' for anchor in sorted(anchors))
    seeds = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
        WHERE words MATCH ? AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        ORDER BY (instr(c.text, ?) > 0) DESC, bm25(words) LIMIT 300
    """, (expression, start_date, end_date, query))
    hits = {}
    for day, bvid, part, start, title, text in seeds:
        nearby = db.execute("""
            SELECT start, text FROM cues WHERE bvid=? AND part=? AND start BETWEEN ? AND ?
              AND instr(text, '♪')=0 ORDER BY start, rowid
        """, (bvid, part, start - 5, start + 5)).fetchall()
        for index, (position, first) in enumerate(nearby):
            first_text = compact(first)
            combined = ""
            excerpts = []
            for when, excerpt in nearby[index:index + 3]:
                if when > position + 5:
                    break
                combined += compact(excerpt)
                excerpts.append(excerpt)
                for offset in range(min(len(first_text), len(combined) - len(query) + 1)):
                    sample = combined[offset:offset + len(query)]
                    if sample == query:
                        rank = 3 if first_text == query else 2
                        status = "整句字幕命中" if rank == 3 else (
                            "跨条字幕命中" if offset + len(query) > len(first_text) else "句内字幕命中"
                        )
                    else:
                        different = [(a, b) for a, b in zip(query, sample) if a != b]
                        if len(different) != 1:
                            continue
                        a, b = different[0]
                        if not sounds.get(a) or sounds[a] != sounds.get(b):
                            continue
                        rank, status = 1, "近音语录候选"
                    key = bvid, part, int(position)
                    row = (day, bvid, part, position, title, " … ".join(excerpts))
                    if key not in hits or rank > hits[key][0]:
                        hits[key] = (rank, row, status)
    return [result(row, status) for _, row, status in sorted(hits.values(), reverse=True)[:limit]]


def roster_entry(query):
    name = re.sub(r"^(?:帮我)?(?:找一下|找找|搜一下|搜索)", "", query).strip()
    name = re.sub(r"^t\s*[.．]\s*", "", name, flags=re.I).strip()
    name = re.sub(r"(?:老师|主播)$", "", name).strip()
    name = re.sub(r"^js\s*[（(]\s*勾[管官观]\s*[）)]$", "JS", name, flags=re.I)
    name = re.sub(r"^三斤\s*[（(]\s*([男女])\s*[）)]$", r"三斤（\1）", name)
    for canonical, entry in people_index().items():
        if name.casefold() in {term.casefold() for term in (canonical, *entry["aliases"])}:
            return entry
    return None


def roster_candidates(entry, start_date, end_date, limit):
    output = []
    for _, day, bvid, part, start, title, excerpt, status, spelling in entry["rows"]:
        if (start_date and day < start_date) or (end_date and day > end_date):
            continue
        output.append({**result((day, bvid, part, start, title, excerpt), status), "matched_spelling": spelling})
        if len(output) == limit:
            break
    return output


def clock_spellings(query):
    match = re.fullmatch(r"(十点半|10点半|10[:：]30)的(地铁|机场|飞机场)", query.strip())
    if not match:
        return ()
    location = match[2]
    locations = ("机场", "飞机场") if location in ("机场", "飞机场") else ("地铁",)
    return tuple(clock + "的" + place
                 for clock in ("十点半", "10点半", "10:30", "10：30")
                 for place in locations)


def literal_variants(db, variants, start_date, end_date, limit, status):
    placeholders = " OR ".join("instr(c.text, ?) > 0" for _ in variants)
    rows = db.execute(f"""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM cues c JOIN videos v USING(bvid)
        WHERE ({placeholders}) AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        ORDER BY v.recording_date DESC, c.bvid, c.part, c.start LIMIT ?
    """, (*variants, start_date, end_date, limit))
    return [result(row, status) for row in rows]


def clock_candidates(db, query, start_date, end_date, limit):
    spellings = clock_spellings(query)
    if not spellings:
        return []
    if spellings[0].endswith("地铁"):
        return literal_variants(db, spellings, start_date, end_date, limit, "歌名字幕线索")
    pattern = r"(?:十点半|10点半|10[:：]30)的(?:那个){0,2}(?:飞机场|机场)"
    rows = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM cues c JOIN videos v USING(bvid)
        WHERE (instr(c.text, '机场') > 0 OR instr(c.text, '飞机场') > 0)
          AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        ORDER BY v.recording_date DESC, c.bvid, c.part, c.start
    """, (start_date, end_date))
    hits = [result(row, "时间写法匹配") for row in rows if re.search(pattern, row[-1])]
    return hits[:limit]


def compact(text):
    return "".join(char for char in text.casefold() if char.isalnum())


def song_query(query):
    quoted = re.search(r"《([^》]+)》", query)
    if quoted:
        title = quoted[1].strip()
        return "十点半的地铁" if "十点半的地铁" in clock_spellings(title) else title
    simplified = compact(query)
    if any(compact(spelling) in simplified for spelling in clock_spellings("十点半的地铁")):
        return "十点半的地铁"
    return next((song for song in sorted(KNOWN_SONGS, key=len, reverse=True)
                 if compact(song) in simplified), None)


def requested_date(query):
    match = re.search(r"(?:(20\d{2})\s*[年/.-]\s*)?(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日|号)?\s*(?:以来|之后|以后|起|到现在|至今)", query)
    if match:
        year, month, day = match.groups()
        return date(int(year) if year else date.today().year, int(month), int(day)).isoformat()


def date_span(value, default_year, default_month=None):
    value = value.strip()
    full = re.fullmatch(r"(\d{4}|\d{2})\s*[年/.-]\s*(\d{1,2})(?:\s*[月/.-]\s*(\d{1,2}))?\s*[月日号]?", value)
    short = re.fullmatch(r"(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*[日号]?", value)
    month_only = re.fullmatch(r"(\d{1,2})\s*月", value)
    day_only = re.fullmatch(r"(\d{1,2})\s*[日号]?", value) if default_month else None
    if full and (len(full[1]) == 4 or full[3] or "年" in value):
        year = int(full[1]) + (2000 if len(full[1]) == 2 else 0)
        month, day = int(full[2]), int(full[3]) if full[3] else None
    elif short:
        year, month, day = default_year, int(short[1]), int(short[2])
    elif month_only:
        year, month, day = default_year, int(month_only[1]), None
    elif day_only:
        year, month, day = default_year, default_month, int(day_only[1])
    else:
        raise ValueError("日期格式无效")
    start = date(year, month, day or 1)
    end = start if day is not None else date(year, month, monthrange(year, month)[1])
    if day == 0:
        raise ValueError("日期格式无效")
    return start, end


def parse_date_range(value, year=None):
    year = date.today().year if year is None else year
    value = value.strip()
    approximate = re.fullmatch(r"(.+?)(?:前后|左右)", value)
    if approximate:
        start, end = date_span(approximate[1], year)
        if start != end:
            raise ValueError("请为前后范围指定某一天")
        return (start - timedelta(days=7)).isoformat(), (end + timedelta(days=7)).isoformat()
    period = re.fullmatch(r"(.+?月)([上中下])旬", value)
    if period:
        start, end = date_span(period[1], year)
        if start != end:
            days = {"上": (1, 10), "中": (11, 20), "下": (21, end.day)}
            first, last = days[period[2]]
            return start.replace(day=first).isoformat(), end.replace(day=last).isoformat()
    # A separator between endpoints can be explicit or a hyphen between slash dates.
    parts = re.split(r"\s*(?:至|到|[~～–—]|\s+-\s+|(?<=\d)-(?=\d{1,2}/|20\d{2}[年/.-]))\s*", value, maxsplit=1)
    if len(parts) == 1 and "/" in value and "-" in value:
        parts = value.split("-", 1)
    start, end = date_span(parts[0], year)
    if len(parts) == 2:
        end = date_span(parts[1], start.year, start.month)[1]
        if end < start:
            raise ValueError("结束日期须在起始日期当天或之后")
    return start.isoformat(), end.isoformat()


def date_listings(start_date, end_date, limit):
    with closing(sqlite3.connect(INDEX.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""
            SELECT v.recording_date, v.bvid,
                   coalesce((SELECT min(c.part) FROM cues c WHERE c.bvid = v.bvid), 1),
                   0, v.title, ''
            FROM videos v
            WHERE v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
            ORDER BY v.recording_date DESC, v.bvid LIMIT ?
        """, (start_date, end_date, limit)).fetchall()
    return [result(row, "当日录屏") for row in rows]


def quote_candidates(db, query, start_date, end_date, limit):
    normalized = compact(query)
    if len(normalized) < 10:
        return []
    size = 4 if len(normalized) < 20 else 5
    anchors = list(dict.fromkeys(normalized[i:i + size] for i in range(0, len(normalized) - size + 1, size)))
    expression = " OR ".join(f'"{anchor}"' for anchor in anchors)
    seeds = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title
        FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
        WHERE words MATCH ? AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        ORDER BY bm25(words) LIMIT 300
    """, (expression, start_date, end_date))
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


def context_candidates(db, concepts, start_date, end_date, limit):
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
          AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        LIMIT 2000
    """, (*params, start_date, end_date))
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

def song_candidates(db, markers, start_date, end_date, limit):
    rows = db.execute("""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
        WHERE words MATCH ? AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
        ORDER BY c.bvid, c.part, c.start
    """, (" OR ".join(f'"{marker}"' for marker in markers), start_date, end_date))
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


def search(query, start_date=None, limit=20, *, end_date=None):
    start_date = start_date or requested_date(query)
    person_query = re.sub(r"(?<![a-z0-9])t\s*[.．]\s*燚|四火|小燚", "", query.lower())
    song = song_query(query)
    quotation = re.search(r'[「“"]([^」”"]+)[」”"]', query)
    speech = quotation[1] if quotation else re.sub(r"^(?:帮我)?(?:找一下|找找|搜一下|搜索)", "", query).strip()
    terms = re.findall(r"[\u3400-\u9fff]{3,}|[a-zA-Z]{2,}", speech)
    with closing(sqlite3.connect(INDEX.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if song == "十点半的地铁":
            return clock_candidates(db, song, start_date, end_date, limit)
        if song or re.search(r"唱(?:过|了)?几次|什么时候唱过", query):
            return song_candidates(db, SONG_LYRICS[song], start_date, end_date, limit) if song in SONG_LYRICS else []
        for person, alias in db.execute("SELECT person, term FROM person_terms ORDER BY length(term) DESC"):
            if alias != person:
                person_query = person_query.replace(alias.lower(), person.lower())
        evidence_query = re.sub(r"勾[管官观]", "js", person_query)
        interaction = re.search(r"知名度|连麦|连线|\bpk\b", person_query)
        two_people = bool(re.search(r"单独|双人|两人|两个人", person_query))
        roster = roster_entry(person_query or query) if not quotation and not interaction else None
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
              AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
            ORDER BY e.evidence_status='字幕线索', v.recording_date DESC LIMIT ?
        """, (evidence_query, interaction[0] if interaction else None, evidence_query,
              interaction[0] if interaction else None, two_people, start_date, end_date, limit)).fetchall()
        if evidence:
            verified = [
                result(row[:-1], "字幕候选" if row[-1] == "字幕线索" else "已有核对记录")
                for row in evidence
            ]
            if roster and len(verified) < limit:
                occupied = {(item["bvid"], item["part"]) for item in verified}
                candidates = roster_candidates(roster, start_date, end_date, limit)
                verified.extend(item for item in candidates if (item["bvid"], item["part"]) not in occupied)
            return verified[:limit]
        if roster:
            return roster_candidates(roster, start_date, end_date, limit)
        if interaction or two_people:
            return []
        concepts = context_terms(query) if not quotation else []
        if len(concepts) >= 2:
            return context_candidates(db, concepts, start_date, end_date, limit)
        if clock_spellings(speech):
            return clock_candidates(db, speech, start_date, end_date, limit)
        if re.fullmatch(r"[\u3400-\u9fff]{5,9}", compact(speech)) and (quotation or not re.search(r"\s", speech)):
            quotes = short_quote_candidates(db, speech, start_date, end_date, limit)
            if quotes:
                return quotes
        if re.fullmatch(r"[\u3400-\u9fff]{2}", speech):
            exact = literal_variants(db, (speech,), start_date, end_date, limit, "字幕原词命中")
            alternatives = ASR_SPELLINGS.get(speech, ())
            if alternatives and len(exact) < limit:
                exact.extend(literal_variants(db, alternatives, start_date, end_date, limit - len(exact), "谐音字幕候选"))
            return exact
        if len(compact(speech)) >= 10:
            return quote_candidates(db, speech, start_date, end_date, limit)
        hits = {}
        for term in terms:
            if len(term) < 3:
                continue
            phrases = [term] if len(term) < 10 else [term[i:i+5] for i in range(0,len(term)-2,4)]
            for phrase in phrases[:12]:
                for day, bvid, part, start, title, text in db.execute("""
                    SELECT v.recording_date,c.bvid,c.part,c.start,v.title,c.text
                    FROM words JOIN cues c ON c.rowid=words.rowid JOIN videos v USING(bvid)
                    WHERE words MATCH ? AND v.recording_date BETWEEN coalesce(?, '') AND coalesce(?, '9999-12-31')
                    ORDER BY bm25(words) LIMIT 150
                """, ('"' + phrase + '"', start_date, end_date)):
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


def group_by_recording_date(results):
    groups = {}
    seen = set()
    for item in results:
        position = (item["bvid"], item["part"], item["seconds"])
        if position in seen:
            continue
        seen.add(position)
        day = item["date"]
        key = (day, "") if day else ("", item["bvid"])
        groups.setdefault(key, []).append(item)
    return [items for _, items in sorted(groups.items(), key=lambda group: group[0][0], reverse=True)]


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
        date_range = params.get("range", [""])[0].strip()
        start_date = params.get("from", [None])[0]
        end_date = params.get("to", [None])[0]
        if len(query) > 160 or len(date_range) > 80 or not (query or date_range or start_date or end_date):
            return self.respond(400, {"error":"请输入时间范围或关键词。"})
        if not INDEX.is_file():
            return self.respond(503, {"error":"搜索索引暂不可用"})
        try:
            if date_range:
                start_date, end_date = parse_date_range(date_range)
            else:
                for bound in (start_date, end_date):
                    if bound and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", bound) or date.fromisoformat(bound).isoformat() != bound):
                        raise ValueError("日期格式无效")
                if not start_date and not end_date and query:
                    try:
                        start_date, end_date = parse_date_range(query)
                    except ValueError:
                        pass
                    else:
                        query = ""
                start_date = start_date or requested_date(query)
            if start_date and end_date and start_date > end_date:
                raise ValueError("起始日期晚于结束日期")
        except ValueError:
            return self.respond(400, {"error": "时间范围无效，请检查日期和顺序，例如 2026/8/10–8/22。"})
        matches = (search(query, start_date, limit=WEB_RESULT_LIMIT + 1, end_date=end_date)
                   if query else date_listings(start_date, end_date, WEB_RESULT_LIMIT + 1))
        results = matches[:WEB_RESULT_LIMIT]
        groups = group_by_recording_date(results)
        payload = {"groups": groups}
        if not groups and roster_entry(query):
            payload["summary"] = "没有足够的主播身份线索。日期、笑声和其他同音词已过滤。"
        if song_query(query) or re.search(r"唱(?:过|了)?几次|什么时候唱过", query):
            scope = (
                f"（{start_date} 至 {end_date}）" if start_date and end_date
                else f"（{start_date} 起）" if start_date
                else f"（截至 {end_date}）" if end_date else ""
            )
            if groups and all(item["status"] == "歌名字幕线索" for item in results):
                payload["summary"] = f"找到歌名线索{scope}，暂无可报告的已核实演唱次数。"
            elif groups:
                payload["summary"] = f"找到歌词候选{scope}，演唱者待核，暂无可报告的已核实次数。"
            else:
                payload["summary"] = f"当前档案暂无可确认的演唱证据{scope}。"
        if len(matches) > WEB_RESULT_LIMIT:
            notice = f"已展示前 {WEB_RESULT_LIMIT} 条匹配片段，可缩小关键词或日期范围继续查找。"
            payload["summary"] = payload.get("summary", "") + notice
        return self.respond(200, payload)

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","9000"))), Handler).serve_forever()
