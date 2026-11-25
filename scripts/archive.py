"""Query a local Xiaoyi subtitle archive using only the Python standard library."""

import argparse
from contextlib import closing
from datetime import date
import gzip
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import NamedTemporaryFile

import archive_core


def prepare(compressed, output):
    if output.exists():
        raise ValueError(f"目标文件已存在，请指定另一个路径：{output}")
    with NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        staging = Path(temporary.name)
        try:
            with gzip.open(compressed, "rb") as source:
                shutil.copyfileobj(source, temporary)
            temporary.close()
            with closing(open_index(staging)) as database:
                database.execute("SELECT text FROM words WHERE words MATCH '姥姥姥' LIMIT 1").fetchall()
            staging.replace(output)
        finally:
            staging.unlink(missing_ok=True)
    return {"db": str(output.resolve()), "bytes": output.stat().st_size}


def open_index(path):
    if not path.is_file():
        raise ValueError(f"找不到字幕索引：{path}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def literal_search(database, phrase, start, end, limit):
    phrase = phrase.strip()
    if not phrase:
        raise ValueError("请输入搜索词。")
    if len(phrase) >= 3:
        source = "words JOIN cues c ON c.rowid=words.rowid"
        predicate = "words MATCH ?"
        term = '"' + phrase.replace('"', '""') + '"'
    else:
        source = "cues c"
        predicate = "instr(c.text, ?) > 0"
        term = phrase
    rows = database.execute(f"""
        SELECT v.recording_date, c.bvid, c.part, c.start, v.title, c.text
        FROM {source} JOIN videos v USING(bvid)
        WHERE {predicate}
          AND (? IS NULL OR v.recording_date >= ?)
          AND (? IS NULL OR v.recording_date <= ?)
        ORDER BY v.recording_date DESC, c.bvid, c.part, c.start LIMIT ?
    """, (term, start, start, end, end, limit + 1)).fetchall()
    return {
        "results": [archive_core.result(row, "字幕候选") for row in rows[:limit]],
        "truncated": len(rows) > limit,
    }


def context(database, bvid, part, seconds, radius):
    video = database.execute(
        "SELECT recording_date, title FROM videos WHERE bvid=?", (bvid,)
    ).fetchone()
    if video is None:
        raise ValueError("档案里没有这个 BV 号。")
    start, end = max(0, seconds - radius), seconds + radius
    rows = database.execute("""
        SELECT start, text FROM cues WHERE bvid=? AND part=? AND start BETWEEN ? AND ?
        ORDER BY start, rowid
    """, (bvid, part, start, end))
    evidence = database.execute("""
        SELECT start_seconds, person, activity, evidence_status, note
        FROM evidence WHERE bvid=? AND part=? AND start_seconds BETWEEN ? AND ?
        ORDER BY start_seconds
    """, (bvid, part, start, end))
    return {
        "date": video[0], "title": video[1], "part": part,
        "url": f"https://www.bilibili.com/video/{bvid}/?p={part}&t={seconds}",
        "cues": [{"seconds": position, "text": text} for position, text in rows],
        "evidence": [
            {"seconds": position, "person": person, "activity": activity, "status": status, "note": note}
            for position, person, activity, status, note in evidence
        ],
    }


def videos_on_day(database, day):
    rows = database.execute("""
        SELECT v.bvid, v.title, v.session_id, c.part, count(c.rowid), max(c.start)
        FROM videos v LEFT JOIN cues c USING(bvid)
        WHERE v.recording_date=? GROUP BY v.bvid, c.part ORDER BY v.bvid, c.part
    """, (day,))
    videos = {}
    for bvid, title, session, part, count, last in rows:
        video = videos.setdefault(bvid, {
            "bvid": bvid, "title": title, "session_id": session,
            "url": f"https://www.bilibili.com/video/{bvid}/", "parts_with_subtitles": [],
        })
        if part is not None:
            video["parts_with_subtitles"].append({"part": part, "cues": count, "last_cue_seconds": last})
    return {"date": day, "videos": list(videos.values())}


def parse_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("请输入 YYYY-MM-DD 日期。") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("xiaoyi-search-v1.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare", help="解压字幕索引")
    setup.add_argument("compressed", type=Path)
    setup.add_argument("--output", type=Path)
    search = commands.add_parser("search", help="搜索关键词")
    search.add_argument("query")
    search.add_argument("--from", dest="start", type=parse_date)
    search.add_argument("--to", dest="end", type=parse_date)
    search.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1–100")
    search.add_argument("--literal", action="store_true")
    nearby = commands.add_parser("context", help="读片段前后的连续字幕")
    nearby.add_argument("bvid")
    nearby.add_argument("part", type=int)
    nearby.add_argument("seconds", type=int)
    nearby.add_argument("--radius", type=int, default=20, choices=range(1, 601), metavar="1–600")
    day = commands.add_parser("day", help="列出某日录屏与字幕分 P")
    day.add_argument("date", type=parse_date)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            output = args.output or args.compressed.with_suffix("")
            response = prepare(args.compressed, output)
        else:
            with closing(open_index(args.db)) as database:
                if args.command == "context":
                    if args.part < 1 or args.seconds < 0:
                        raise ValueError("P 数必须至少为 1，秒数必须至少为 0。")
                    response = context(database, args.bvid, args.part, args.seconds, args.radius)
                elif args.command == "day":
                    response = videos_on_day(database, args.date)
                elif args.literal:
                    if args.start and args.end and args.start > args.end:
                        raise ValueError("起始日期必须早于或等于结束日期。")
                    response = literal_search(database, args.query, args.start, args.end, args.limit)
                else:
                    if args.end:
                        raise ValueError("结束日期请与 --literal 一起使用。")
                    archive_core.INDEX = args.db
                    results = archive_core.search(args.query, args.start, limit=args.limit + 1)
                    response = {
                        "groups": archive_core.group_results(results[:args.limit]),
                        "truncated": len(results) > args.limit,
                    }
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
