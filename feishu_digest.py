#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""搜广推顶会论文每日推送（自包含版）

链路：OpenAlex 会议元数据 -> 关键词/主题筛选 -> 彩云小译翻译 -> 飞书机器人卡片。

特点：
  * 不依赖任何私有接口（无需 CONF_URL），也不需要 Git LFS；
  * 只提交一个几百 KB 的状态文件，用来避免重复推送；
  * 数据源 OpenAlex 免费、无需 API Key（建议填 OPENALEX_MAILTO 进入礼貌池）。

常用环境变量：
  FEISHU_URL        飞书机器人 webhook，多个用英文逗号分隔（必填）
  CAIYUN_TOKEN      彩云小译 token（不填则只推英文原文）
  CONFS             目标会议，逗号分隔，默认 sigir,kdd,www,cikm,recsys,wsdm
  LIMITS            本次最多推送篇数，默认 5
  YEARS             向前回溯的会议年份数，默认 2
  MIN_SCORE         相关性阈值，默认 1.0
  DRY_RUN           true 时只打印不发送
  OPENALEX_MAILTO   你的邮箱，OpenAlex 礼貌池标识（可选）
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

OPENALEX_API = "https://api.openalex.org"
CAIYUN_API = "https://api.interpreter.caiyunai.com/v1/translator"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "feishu-paper-digest/1.0"})

# OpenAlex 中的会议 source ID（已逐个核对过 volumes 与年份）
CONF_SOURCES = {
    "sigir": "S4363608773",
    "kdd": "S4363608767",
    "www": "S7407088480",
    "cikm": "S4393918199",
    "recsys": "S7407086088",
    "wsdm": "S4363608885",
}
# ECIR 在 OpenAlex 的收录很稀疏，如需启用可自行补充 source id
DEFAULT_CONFS = ["sigir", "kdd", "www", "cikm", "recsys", "wsdm"]

# 搜广推强相关主题（OpenAlex topics）
RELEVANT_TOPICS = {
    "recommender systems and techniques",
    "information retrieval and search behavior",
    "advanced bandit algorithms research",
    "computational advertising and pricing",
    "web search and information retrieval",
    "e-commerce and digital marketing",
    "online advertising and marketing",
}

# 标题关键词：主关键词每个 +1，次关键词每个 +0.25
PRIMARY_KEYWORDS = [
    "recommend", "recommender", "recommendation", "ctr", "cvr", "click-through", "click through",
    "retriev", "search", "ranking", "ranker", "re-rank", "rerank", "match", "query", "relevance",
    "advertis", "auction", "bid", "budget", "impression", "conversion", "personaliz",
    "collaborative filtering", "sequential", "multi-task", "multi-objective", "multi-goal",
    "candidate generation", "embedding", "cold-start", "cold start", "session", "user behavior",
    "bandit", "uplift", "look-alike", "lookalike", "feature crossing", "knowledge distillation",
]
SECONDARY_KEYWORDS = [
    "user", "item", "interest", "intent", "feed", "negative sampling", "contrastive",
    "graph neural", "transformer", "llm", "large language", "cross-domain", "transfer",
    "debias", "bias", "diversif", "explainab", "online", "stream", "real-time", "industrial",
]

# 明显不是研究论文的条目标题
NOISE_PATTERNS = re.compile(
    r"^(?:(?:a|an|the)\s+)?("
    r"front matter|frontmatter|index|author index|subject index|preface|foreword|editorial|"
    r"message from|welcome|committees?|organizing committee|program committee|sponsors?|"
    r"table of contents|contents|proceedings of|conference report|opening|closing|"
    r"keynote|panel\b|tutorial|doctoral consortium|demo\b|demonstrations?\b|posters?\b|workshop|"
    r"appendix|erratum|correction|reply to|in memoriam|obituary"
    r")",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}


def parse_list(name, default=None):
    raw = os.environ.get(name, "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or list(default or [])


def log(msg):
    print(msg, flush=True)


def request_json(url, params=None, retries=4, timeout=45):
    """带退避重试的 GET，返回 JSON。"""
    last_error = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt * 3
                log(f"  [warn] HTTP {resp.status_code}，{wait}s 后重试（{attempt + 1}/{retries}）")
                time.sleep(wait)
                last_error = f"HTTP {resp.status_code}"
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.RequestException as exc:
            last_error = str(exc)
            wait = 2 ** attempt * 2
            log(f"  [warn] 请求失败：{exc}，{wait}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"请求失败：{url} ({last_error})")


def rebuild_abstract(inverted_index):
    """OpenAlex 摘要以「词 -> 位置」倒排索引存储，这里还原成正文。"""
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for idx in idxs:
            positions[idx] = word
    return " ".join(positions[i] for i in sorted(positions))


def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def paper_key(conf, year, work):
    doi = (work.get("doi") or "").lower().replace("https://doi.org/", "")
    raw = doi or work.get("id") or work.get("display_name", "")
    return f"{conf}{year}:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]}"


# --------------------------------------------------------------------------- #
# OpenAlex 取数
# --------------------------------------------------------------------------- #
def fetch_conference_year(conf, year, mailto):
    """拉取某会议某年的全部论文（不含摘要；cursor 分页现需 API Key，故用 page 分页）。"""
    source_id = CONF_SOURCES[conf]
    works, page = [], 1
    fields = ("id,doi,display_name,publication_year,publication_date,authorships,"
              "primary_topic,topics,keywords,cited_by_count,type,is_paratext")
    while page <= 10:
        params = {
            "filter": f"locations.source.id:{source_id},publication_year:{year}",
            "per-page": 200,
            "page": page,
            "select": fields,
        }
        if mailto:
            params["mailto"] = mailto
        data = request_json(f"{OPENALEX_API}/works", params)
        batch = data.get("results") or []
        works.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return works


def fetch_abstracts(work_ids, mailto):
    """按 id 批量取摘要，返回 {work_id: abstract}。"""
    abstracts = {}
    for i in range(0, len(work_ids), 40):
        chunk = work_ids[i:i + 40]
        params = {
            "filter": "openalex:" + "|".join(chunk),
            "per-page": 50,
            "select": "id,abstract_inverted_index",
        }
        if mailto:
            params["mailto"] = mailto
        try:
            data = request_json(f"{OPENALEX_API}/works", params)
        except RuntimeError as exc:
            log(f"  [warn] 批量取摘要失败，跳过本批：{exc}")
            continue
        for work in data.get("results") or []:
            work_id = (work.get("id") or "").rstrip("/").split("/")[-1]
            abstracts[work_id] = rebuild_abstract(work.get("abstract_inverted_index"))
        time.sleep(0.3)
    return abstracts


# --------------------------------------------------------------------------- #
# 相关性打分
# --------------------------------------------------------------------------- #
def relevance_score(work):
    title = (work.get("display_name") or "").lower()
    score = 0.0
    for keyword in PRIMARY_KEYWORDS:
        if keyword in title:
            score += 1.0
    for keyword in SECONDARY_KEYWORDS:
        if keyword in title:
            score += 0.25

    for topic in (work.get("topics") or [])[:3]:
        if (topic.get("display_name") or "").lower() in RELEVANT_TOPICS:
            score += 3.0
            break

    for keyword in (work.get("keywords") or [])[:6]:
        name = (keyword.get("display_name") or "").lower()
        if any(k in name for k in ("recommend", "retrieval", "search", "advertis", "ranking", "click", "ctr", "bandit", "query")):
            score += 0.5
    return round(score, 2)


def is_noise(work):
    title = (work.get("display_name") or "").strip()
    if not title or work.get("is_paratext"):
        return True
    if work.get("type") in {"paratext", "erratum", "letter", "editorial"}:
        return True
    return bool(NOISE_PATTERNS.match(title))


def paper_url(work):
    doi = work.get("doi")
    if doi:
        return doi
    return (work.get("id") or "").replace("https://api.openalex.org/works/", "https://openalex.org/works/")


def author_names(work, limit=6):
    names = []
    for authorship in work.get("authorships") or []:
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            names.append(name)
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" 等 {len(names)} 人"
    return ", ".join(names)


def institutions(work, limit=3):
    seen = []
    for authorship in work.get("authorships") or []:
        for inst in authorship.get("institutions") or []:
            name = (inst.get("display_name") or "").strip()
            if name and name not in seen:
                seen.append(name)
    if not seen:  # OpenAlex 未结构化时退回原始署名
        for authorship in work.get("authorships") or []:
            for raw in authorship.get("raw_affiliation_strings") or []:
                name = (raw or "").strip()
                if name and name not in seen:
                    seen.append(name)
    return "; ".join(seen[:limit])


def clip(text, limit):
    """按句子/单词边界截断，避免把摘要切成半个词。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "！", "？", ". ", "；", "; "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[:pos + len(sep)].rstrip() + "…"
    pos = cut.rfind(" ")
    if pos > limit * 0.6:
        return cut[:pos].rstrip() + "…"
    return cut.rstrip() + "…"


# --------------------------------------------------------------------------- #
# 翻译（彩云小译）
# --------------------------------------------------------------------------- #
def caiyun_translate(texts, token, chunk_chars=4000, chunk_items=10):
    """批量翻译，按字符数/条数切块；失败位置用空字符串占位。"""
    if not token or not texts:
        return ["" for _ in texts]

    results = [""] * len(texts)
    start = 0
    while start < len(texts):
        end, size = start, 0
        while end < len(texts) and end - start < chunk_items and (size < chunk_chars or end == start):
            size += len(texts[end])
            end += 1

        payload = {
            "source": texts[start:end],
            "trans_type": "en2zh",
            "request_id": "feishu_digest",
            "detect": True,
        }
        headers = {"content-type": "application/json", "x-authorization": "token " + token}
        for attempt in range(3):
            try:
                resp = SESSION.post(CAIYUN_API, data=json.dumps(payload), headers=headers, timeout=60)
                if resp.status_code == 200:
                    targets = resp.json().get("target") or []
                    if len(targets) == end - start:
                        results[start:end] = targets
                    else:
                        log(f"  [warn] 彩云返回条数不符（{len(targets)} != {end - start}）")
                    break
                if resp.status_code in (429, 500, 502, 503):
                    time.sleep(2 ** attempt * 3)
                    continue
                log(f"  [warn] 彩云翻译失败 HTTP {resp.status_code}: {resp.text[:120]}")
                break
            except requests.RequestException as exc:
                log(f"  [warn] 彩云翻译异常：{exc}")
                time.sleep(2 ** attempt * 2)
        start = end
        time.sleep(0.2)
    return results


# --------------------------------------------------------------------------- #
# 飞书
# --------------------------------------------------------------------------- #
def send_feishu(urls, title, content, dry_run=False):
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title[:120]}},
        "elements": [{"tag": "markdown", "content": content}],
    }
    body = json.dumps({"msg_type": "interactive", "card": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)

    if dry_run:
        log(f"[DRY_RUN] 将发送：{title}")
        return True

    ok = True
    for url in urls:
        for attempt in range(2):
            try:
                resp = SESSION.post(url, data=body.encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, timeout=15)
                data = resp.json() if resp.content else {}
                code = data.get("code", data.get("StatusCode"))
                if resp.status_code == 200 and code in (0, None):
                    break
                log(f"  [warn] 飞书返回 HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:150]}")
                ok = False
            except (requests.RequestException, ValueError) as exc:
                log(f"  [warn] 飞书发送异常：{exc}")
                ok = False
            time.sleep(2)
    return ok


def build_card(conf, year, work, abstract, translation):
    title_en = work.get("display_name") or ""
    authors = author_names(work) or "NA"
    orgs = institutions(work) or "NA"
    url = paper_url(work)
    cites = work.get("cited_by_count") or 0
    topic = ((work.get("primary_topic") or {}).get("display_name") or "").strip()

    title_zh = translation.get("title") or title_en
    abstract_zh = translation.get("abstract") or ""

    head = [
        f"**{title_en}**",
        "",
        f"**作者**：{authors}",
        f"**机构**：{orgs}",
        f"**会议**：{conf.upper()} {year}" + (f" · {topic}" if topic else "") + f" · 引用 {cites}",
        f"**链接**：[论文主页]({url})",
    ]
    parts = ["\n".join(head)]
    if abstract_zh:
        parts.append("**📝 中文摘要**\n" + clip(abstract_zh, 1200))
    if abstract:
        parts.append("**📄 Abstract**\n" + clip(abstract, 700))

    content = "\n\n".join(parts)
    if len(content) > 6000:
        content = content[:6000] + "…"
    return title_zh[:80] + f" · {conf.upper()} {year}", content


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
def load_state(path):
    if not path.exists():
        return {"version": 1, "pushed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("pushed", {})
        return data
    except (ValueError, OSError):
        return {"version": 1, "pushed": {}}


def save_state(path, state, limit=30000):
    pushed = state.get("pushed", {})
    if len(pushed) > limit:
        newest = sorted(pushed.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        state["pushed"] = dict(newest)
    state["version"] = 1
    state["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def collect_candidates(confs, years, min_score, pushed, mailto):
    """按会议收集候选（每个会议取最新有数据、且还有未推送论文的年份）。"""
    now_year = datetime.now().year
    candidates = {}
    for conf in confs:
        if conf not in CONF_SOURCES:
            log(f"[{conf}] 未配置 OpenAlex source id，跳过")
            continue
        for year in range(now_year, now_year - years, -1):
            works = fetch_conference_year(conf, year, mailto)
            if not works:
                log(f"[{conf.upper()} {year}] 无数据")
                continue
            fresh = []
            for work in works:
                if is_noise(work):
                    continue
                if paper_key(conf, year, work) in pushed:
                    continue
                score = relevance_score(work)
                if score < min_score:
                    continue
                fresh.append((score, work))
            log(f"[{conf.upper()} {year}] 共 {len(works)} 篇，过滤后待推 {len(fresh)} 篇")
            if fresh:
                fresh.sort(key=lambda item: (-item[0], -(item[1].get("cited_by_count") or 0)))
                candidates[conf] = {"year": year, "items": fresh}
                break
    return candidates


def pick(candidates, limits):
    """各会议轮转挑选，保证每天的内容有来源多样性。"""
    chosen, index = [], 0
    while len(chosen) < limits:
        added = False
        for conf, bucket in candidates.items():
            if index < len(bucket["items"]) and len(chosen) < limits:
                score, work = bucket["items"][index]
                chosen.append((conf, bucket["year"], score, work))
                added = True
        if not added:
            break
        index += 1
    return chosen


def main():
    parser = argparse.ArgumentParser(description="搜广推顶会论文每日推送")
    parser.add_argument("--state", type=Path, default=Path(__file__).resolve().parent / "data" / "digest_state.json")
    parser.add_argument("--limits", type=int, default=int(os.environ.get("LIMITS", "5")))
    parser.add_argument("--years", type=int, default=int(os.environ.get("YEARS", "2")))
    parser.add_argument("--min-score", type=float, default=float(os.environ.get("MIN_SCORE", "1.0")))
    parser.add_argument("--push-interval", type=float, default=float(os.environ.get("PUSH_INTERVAL", "3")))
    parser.add_argument("--dry-run", action="store_true", default=env_bool("DRY_RUN", False))
    parser.add_argument("--no-translate", action="store_true")
    args = parser.parse_args()

    feishu_urls = parse_list("FEISHU_URL")
    if not feishu_urls and not args.dry_run:
        log("::error::未配置 FEISHU_URL")
        return 1

    confs = [c.lower() for c in parse_list("CONFS", DEFAULT_CONFS)]
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    token = "" if args.no_translate else os.environ.get("CAIYUN_TOKEN", "").strip()

    state = load_state(args.state)
    pushed = state.setdefault("pushed", {})
    log(f"已推送记录：{len(pushed)} 条；目标会议：{', '.join(confs)}；本次上限 {args.limits} 篇")

    candidates = collect_candidates(confs, args.years, args.min_score, pushed, mailto)
    if not candidates:
        log("目标会议没有可推送的新论文（可能已全部推送完，或阈值过高）")
        return 0

    chosen = pick(candidates, args.limits)
    if not chosen:
        log("没有选中任何论文")
        return 0
    log(f"本次选中 {len(chosen)} 篇：" + "; ".join(f"{c.upper()}{y}" for c, y, _, _ in chosen))

    abstracts = fetch_abstracts([w["id"].rstrip("/").split("/")[-1] for _, _, _, w in chosen], mailto)

    texts, mapping = [], []
    for conf, year, score, work in chosen:
        work_id = work["id"].rstrip("/").split("/")[-1]
        abstract = abstracts.get(work_id, "")
        title_en = work.get("display_name") or ""
        texts.extend([title_en, abstract or title_en])
        mapping.append((conf, year, work, abstract))

    log("调用彩云小译翻译标题与摘要 ...")
    translated = caiyun_translate(texts, token) if token else [""] * len(texts)
    if not token:
        log("  [warn] 未配置 CAIYUN_TOKEN，仅推送英文原文")

    sent = 0
    today = datetime.now().strftime("%Y-%m-%d")
    for i, (conf, year, work, abstract) in enumerate(mapping):
        translation = {"title": translated[2 * i], "abstract": translated[2 * i + 1]}
        title, content = build_card(conf, year, work, abstract, translation)
        if send_feishu(feishu_urls, title, content, args.dry_run):
            sent += 1
            if not args.dry_run:
                pushed[paper_key(conf, year, work)] = today
        else:
            log(f"  [warn] 推送失败：{work.get('display_name')}")
        if not args.dry_run:
            time.sleep(args.push_interval)

    if not args.dry_run:
        save_state(args.state, state)
        log(f"状态已保存：{args.state}")

    log(f"完成：成功推送 {sent}/{len(mapping)} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
