"""Publish a Chinese daily report to Telegraph, then send its link to Telegram.

Run with: uv run --with markdown-it-py python scripts/publish_telegram.py
"""

import argparse
import html
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt


BRAND = "北极星AI"
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "em", "h3", "h4", "hr",
    "i", "img", "li", "ol", "p", "pre", "s", "strong", "u", "ul",
}
TAG_MAP = {"h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4", "tr": "p"}


class TelegraphParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = [(None, self.nodes)]

    def handle_starttag(self, tag, attrs):
        mapped = TAG_MAP.get(tag, tag)
        children = self.stack[-1][1]
        if mapped not in ALLOWED_TAGS:
            self.stack.append((tag, children))
            return
        node = {"tag": mapped}
        safe_attrs = {}
        for key, value in attrs:
            if (mapped, key) in {("a", "href"), ("img", "src")}:
                if urlparse(value or "").scheme in {"http", "https"}:
                    safe_attrs[key] = value
        if mapped == "img" and "src" not in safe_attrs:
            return
        if safe_attrs:
            node["attrs"] = safe_attrs
        children.append(node)
        if mapped not in {"br", "hr", "img"}:
            node["children"] = []
            self.stack.append((tag, node["children"]))

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.stack[-1][1].append(" | ")
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if data.strip() or len(self.stack) > 1:
            self.stack[-1][1].append(data)


def api_request(url, fields):
    request = Request(url, data=urlencode(fields).encode("utf-8"), method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except HTTPError as error:
        # Never include request URLs here: Telegram URLs contain the bot token.
        raise RuntimeError(f"接口请求失败，HTTP {error.code}") from None
    except (URLError, TimeoutError):
        raise RuntimeError("接口连接失败或超时，请检查运行日志中的发布阶段") from None
    if not payload.get("ok"):
        description = str(payload.get("description") or payload.get("error") or "未知错误")
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAPH_ACCESS_TOKEN"):
            secret = os.environ.get(name)
            if secret:
                description = description.replace(secret, "***")
        raise RuntimeError(f"接口返回错误：{description}")
    return payload["result"]


def select_report(explicit_path):
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise RuntimeError("指定的日报文件不存在")
        return path
    candidates = []
    for path in Path("data/summaries").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", text))
        if chinese_count >= 20 and chinese_count / max(len(text), 1) >= 0.1:
            candidates.append(path)
    if not candidates:
        raise RuntimeError("没有找到中文日报，可用 --report 指定 Markdown 文件")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def prepare_report(path):
    text = path.read_text(encoding="utf-8").strip()
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    # Rebrand visible text while preserving source URLs and link destinations.
    text = re.sub(r"https?://[^\s<>\)]+|\bHorizon\b",
                  lambda match: BRAND if match.group().lower() == "horizon" else match.group(),
                  text, flags=re.I)
    lines = text.splitlines()
    if lines and re.match(r"^#\s+", lines[0]):
        text = "\n".join(lines[1:]).strip()
    if not text:
        raise RuntimeError("日报正文为空")
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
    parser = TelegraphParser()
    parser.feed(renderer.render(text))
    content = json.dumps(parser.nodes, ensure_ascii=False, separators=(",", ":"))
    if len(content.encode("utf-8")) > 64000:
        raise RuntimeError("日报超过 Telegraph 单篇容量，请减少 digest.max_items 后重新生成")
    # Extract real article titles from Markdown links, without another AI call.
    titles = []
    for label in re.findall(r"\[([^\]\n]+)\]\(https?://[^\s)]+\)", text):
        label = re.sub(r"[*_`]+", "", label).strip()
        if len(label) >= 8 and label not in titles:
            titles.append(label[:100])
    return content, titles[:5]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", help="明确指定一份 Markdown 日报")
    parser.add_argument("--dry-run", action="store_true", help="验证转换，不发布或发送")
    args = parser.parse_args()
    report = select_report(args.report)
    content, titles = prepare_report(report)
    date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y年%m月%d日")
    title = f"{BRAND} 日报｜{date}"
    print(f"选择日报：{report}")
    print(f"文章标题：{title}，正文 {len(content.encode('utf-8'))} 字节")
    if args.dry_run:
        print("转换验证通过，未调用发布接口")
        return
    required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAPH_ACCESS_TOKEN")
    for name in required:
        if not os.environ.get(name):
            raise RuntimeError(f"缺少环境变量 {name}")
    print("正在发布 Telegraph 文章")
    page = api_request("https://api.telegra.ph/createPage", {
        "access_token": os.environ["TELEGRAPH_ACCESS_TOKEN"],
        "title": title,
        "author_name": BRAND,
        "content": content,
        "return_content": "false",
    })
    url = page["url"]
    print(f"文章已发布：{url}")
    highlights = "\n".join(f"• {html.escape(item)}" for item in titles)
    message = f"🌟 <b>{html.escape(title)}</b>\n\n"
    message += (highlights + "\n\n") if highlights else "今日 AI 与科技资讯已更新。\n\n"
    message += f'<a href="{html.escape(url, quote=True)}">📖 点击阅读完整日报</a>'
    fields = {
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": message,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": False, "url": url}),
        "reply_markup": json.dumps({"inline_keyboard": [[{
            "text": "📖 阅读完整日报", "url": url,
        }]]}, ensure_ascii=False),
    }
    if os.environ.get("TELEGRAM_THREAD_ID"):
        fields["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
    print("正在发送 Telegram 阅读卡片")
    api_request(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage", fields)
    print("北极星AI 阅读卡片发送成功")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"发布失败：{error}", file=sys.stderr)
        sys.exit(1)
