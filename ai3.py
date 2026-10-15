"""
mini_agent.py —— 命令行交互 + 分层记忆 + 专业工具 的迷你 Agent  (v2)

v2 新增：
  - read_file   带行号、可分段读取（大文件不撑爆上下文）
  - edit_file   精确替换：old_str 必须唯一匹配，且必须先读后改
  - write_file  新建/整体覆盖文件
  - search_code 在文件内容里搜索（正则），结果数量受限
  - find_files  按通配符找文件
  - web_search  联网搜索（国内可用：博查 / 智谱 / 必应中国，按顺序自动尝试，失败自动换下一个）
  - web_fetch   抓取网页正文
  - 命令安全分级：直接拒绝 / 需要确认 / 放行；确认时可选“本会话一直允许”

运行：
  pip install openai requests trafilatura     # trafilatura 可选，用于更干净地提取网页正文
  export LLM_API_KEY=你的key
  export LLM_BASE_URL=https://api.deepseek.com      # 通义：https://dashscope.aliyuncs.com/compatible-mode/v1
  export LLM_MODEL=deepseek-chat                    # 通义：qwen-plus
  export BOCHA_API_KEY=可选          # 博查搜索 https://open.bochaai.com 国内直连，专为 AI 设计，推荐
  export ZHIPU_API_KEY=可选          # 智谱搜索（智谱开放平台 key），国内直连
  # 以上都不配置时，自动使用免费的“必应中国”网页搜索（无需 key，但稳定性一般）
  export TAVILY_API_KEY=可选                         # 国外服务，国内网络可能连不上，一般不用配
  python mini_agent.py
"""
import difflib
import fnmatch
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from openai import OpenAI

try:
    import readline  # 输入支持 ↑↓ 翻历史（Windows 下没有也不影响）
except ImportError:
    pass

# ====================== 配置 ======================
API_KEY = os.getenv("LLM_API_KEY", "sk-c0f724d19069479d8572f5ff5245ca34")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
TAVILY_KEY = os.getenv("TAVILY_API_KEY", "")
BOCHA_KEY = os.getenv("BOCHA_API_KEY", "")
ZHIPU_KEY = os.getenv("ZHIPU_API_KEY", "")

WORKSPACE = Path("./workspace").resolve()
DATA_DIR = Path("./.agent")
SESSIONS_DIR = DATA_DIR / "sessions"
MEMORY_FILE = DATA_DIR / "memory.md"
for d in (WORKSPACE, SESSIONS_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_STEPS = 40             # 单个任务最多循环几圈
CONTEXT_LIMIT = 50_000     # prompt token 超过就压缩
KEEP_RECENT_TURNS = 3      # 压缩时保留最近几轮
TOOL_OUTPUT_LIMIT = 8000   # 命令输出最多保留字符数
READ_DEFAULT_LINES = 300   # read_file 默认一次读多少行
MAX_LINE_CHARS = 500       # 单行超长截断（压缩过的 js/css 一行可能几 MB）
SEARCH_MAX_RESULTS = 80    # search_code 最多返回多少条
FETCH_MAX_CHARS = 10_000   # web_fetch 一次最多返回多少字符
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".agent"}

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ====================== 命令安全分级 ======================
# 第一级：直接拒绝，问都不问
DENY_PATTERNS = [
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)\s*($|;|&)",  # rm -rf /  rm -rf ~
    r"\bmkfs\b", r"\bdd\s+.*of=/dev/", r":\(\)\s*\{.*\};\s*:",   # 格式化 / 写裸盘 / fork 炸弹
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r">\s*/dev/sd[a-z]",
]
# 第二级：需要用户确认。(正则, 类别名) —— 类别名用于“本会话一直允许”
CONFIRM_PATTERNS = [
    (r"\brm\b", "删除文件"),
    (r"\bmv\b", "移动/重命名"),
    (r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+--|rebase|branch\s+-D)", "危险 git 操作"),
    (r"\bsudo\b", "sudo 提权"),
    (r"\bchmod\b|\bchown\b", "修改权限"),
    (r"\b(kill|pkill|killall)\b", "结束进程"),
    (r"(curl|wget)[^|]*\|\s*(ba|z)?sh", "下载并执行脚本"),
    (r"\b(pip|npm|brew|apt(-get)?)\s+(uninstall|remove)", "卸载软件"),
    (r"\btruncate\b|>\s*[^\s&|>]+", "覆盖写文件"),   # 用 > 重定向可能会覆盖已有文件
]
ALWAYS_ALLOWED: set[str] = set()   # 本会话中用户选择“一直允许”的类别


def ask_user(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "n"


def check_command(command: str) -> str | None:
    """返回 None 表示放行，否则返回拒绝原因（会作为工具结果交给模型）"""
    for p in DENY_PATTERNS:
        if re.search(p, command):
            print(f"  ⛔ 已拦截高危命令：{command}")
            return "该命令属于高危操作，已被系统直接拒绝。请换一种安全的方式完成任务。"
    hits = [cat for p, cat in CONFIRM_PATTERNS if re.search(p, command) and cat not in ALWAYS_ALLOWED]
    if not hits:
        return None
    print(f"  ⚠️  模型想执行：{command}")
    ans = ask_user(f"  涉及【{'、'.join(hits)}】。允许吗？ y=允许一次 / a=本会话此类都允许 / n=拒绝 > ")
    if ans == "a":
        ALWAYS_ALLOWED.update(hits)
        return None
    if ans == "y":
        return None
    reason = ask_user("  （可选）告诉模型拒绝原因或替代做法，直接回车跳过 > ")
    return "用户拒绝执行该命令。" + (f"用户说明：{reason}" if reason else "请换一种方式，或先向用户说明意图。")


# ====================== 路径工具 ======================
def resolve(path: str) -> Path:
    """相对路径一律相对于工作目录"""
    p = Path(path).expanduser()
    return (p if p.is_absolute() else WORKSPACE / p).resolve()


def confirm_outside_write(p: Path) -> str | None:
    """写工作目录以外的文件需要确认"""
    if p == WORKSPACE or WORKSPACE in p.parents:
        return None
    if "写工作目录外" in ALWAYS_ALLOWED:
        return None
    ans = ask_user(f"  ⚠️  模型想写入工作目录以外的文件：{p}\n  允许吗？ y / a=本会话都允许 / n > ")
    if ans == "a":
        ALWAYS_ALLOWED.add("写工作目录外")
        return None
    return None if ans == "y" else "用户拒绝写入工作目录以外的位置，请把文件写到工作目录内。"


def is_binary(p: Path) -> bool:
    with p.open("rb") as f:
        return b"\0" in f.read(8192)


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(WORKSPACE))
    except ValueError:
        return str(p)


# ====================== 文件工具：读 / 改 / 写 ======================
# 记录模型读过的文件及当时的修改时间，用于“先读后改”检查
READ_STATE: dict[str, float] = {}


def read_file(path: str, offset: int = 1, limit: int = READ_DEFAULT_LINES) -> str:
    p = resolve(path)
    if not p.exists():
        return f"文件不存在：{path}。可以用 find_files 查找正确路径。"
    if p.is_dir():
        return f"{path} 是目录，请用 find_files 或 run_command ls 查看。"
    if is_binary(p):
        return f"{path} 是二进制文件（{p.stat().st_size} 字节），无法按文本读取，请用 run_command 配合专门工具处理。"

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 2000))
    chunk = lines[offset - 1: offset - 1 + limit]

    out = []
    for i, line in enumerate(chunk, start=offset):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f"…（该行共 {len(line)} 字符，已截断）"
        out.append(f"{i:>6}→{line}")   # 行号 + 箭头 + 原文，箭头后的内容才是文件真实内容

    READ_STATE[str(p)] = p.stat().st_mtime
    end = offset + len(chunk) - 1
    header = f"[{rel(p)}] 共 {total} 行，当前显示第 {offset}-{end} 行"
    if end < total:
        header += f"。还有更多内容，可用 offset={end + 1} 继续读取"
    return header + "\n" + ("\n".join(out) if out else "（空文件或超出范围）")


def show_diff(old: str, new: str, name: str):
    """在终端给用户看改了什么（不发给模型，节省上下文）"""
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
    for line in diff[:40]:
        color = "\033[32m" if line.startswith("+") else "\033[31m" if line.startswith("-") else "\033[90m"
        print(f"    {color}{line}\033[0m")
    if len(diff) > 40:
        print(f"    …（还有 {len(diff) - 40} 行变更）")


def edit_file(path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    p = resolve(path)
    if not p.exists():
        return f"文件不存在：{path}。新建文件请用 write_file。"
    # ---- 规则 1：必须先读后改 ----
    if str(p) not in READ_STATE:
        return "修改前必须先用 read_file 读取该文件，确认当前内容后再编辑。"
    # ---- 规则 2：读完之后文件没被别人改过 ----
    if p.stat().st_mtime != READ_STATE[str(p)]:
        return "该文件在你上次读取之后被修改过（可能是命令或用户改的），请重新 read_file 后再编辑。"
    if old_str == new_str:
        return "old_str 和 new_str 相同，没有需要修改的内容。"
    if not old_str:
        return "old_str 不能为空。如果要整体重写文件请用 write_file。"

    content = p.read_text(encoding="utf-8")
    count = content.count(old_str)
    # ---- 规则 3：必须精确且唯一匹配；报错信息要告诉模型怎么改 ----
    if count == 0:
        hint = ""
        first = old_str.strip().splitlines()[0].strip() if old_str.strip() else ""
        if first and first in content:
            ln = content[:content.index(first)].count("\n") + 1
            hint = f"提示：old_str 的第一行在第 {ln} 行附近出现过，可能是缩进、空格或后续行不一致。"
        return ("在文件中找不到 old_str。注意：old_str 必须与文件内容逐字一致（包括缩进和空格），"
                "且不要包含 read_file 输出中的行号和 → 前缀。" + hint + " 建议重新 read_file 对应区域。")
    if count > 1 and not replace_all:
        lines = [content[:m.start()].count("\n") + 1 for m in re.finditer(re.escape(old_str), content)]
        return (f"old_str 在文件中出现了 {count} 次（第 {lines[:10]} 行），无法确定改哪一处。"
                "请在 old_str 中包含更多上下文使其唯一；如果确实要全部替换，设置 replace_all=true。")

    new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
    p.write_text(new_content, encoding="utf-8")
    READ_STATE[str(p)] = p.stat().st_mtime   # 自己改的不算“被别人改过”

    print(f"  ✏️  编辑 {rel(p)}（{count if replace_all else 1} 处）")
    show_diff(content, new_content, rel(p))
    # 把修改位置附近的内容回传给模型，方便它确认改对了
    pos = new_content.find(new_str) if new_str else 0
    ln = new_content[:pos].count("\n") + 1
    snippet = new_content.splitlines()[max(0, ln - 3): ln + new_str.count("\n") + 2]
    numbered = "\n".join(f"{i:>6}→{l[:MAX_LINE_CHARS]}" for i, l in enumerate(snippet, start=max(1, ln - 2)))
    return f"已修改 {rel(p)}，替换 {count if replace_all else 1} 处。修改后附近内容：\n{numbered}"


def write_file(path: str, content: str) -> str:
    p = resolve(path)
    if (err := confirm_outside_write(p)):
        return err
    if p.exists():
        # 覆盖已有文件同样要求先读过，避免模型凭印象把整个文件重写坏
        if str(p) not in READ_STATE:
            return "该文件已存在。覆盖前必须先 read_file；如果只是局部修改，请用 edit_file。"
        old = p.read_text(encoding="utf-8", errors="replace")
        print(f"  📝 覆盖 {rel(p)}")
        show_diff(old, content, rel(p))
    else:
        print(f"  📝 新建 {rel(p)}（{content.count(chr(10)) + 1} 行）")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    READ_STATE[str(p)] = p.stat().st_mtime
    return f"已写入 {rel(p)}，共 {content.count(chr(10)) + 1} 行"


# ====================== 搜索工具：找文件 / 搜内容 ======================
def walk_files(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            yield Path(root) / f


def find_files(pattern: str, path: str = ".") -> str:
    base = resolve(path)
    if not base.is_dir():
        return f"目录不存在：{path}"
    # 支持 "*.py" 和 "src/**/*.ts" 两种写法
    matches = [f for f in walk_files(base)
               if fnmatch.fnmatch(f.name, pattern) or fnmatch.fnmatch(str(f.relative_to(base)), pattern)]
    matches.sort(key=lambda f: f.stat().st_mtime, reverse=True)   # 最近修改的排前面
    if not matches:
        return f"没有找到匹配 {pattern} 的文件"
    shown = matches[:100]
    lines = [f"{rel(f)}  ({f.stat().st_size} 字节)" for f in shown]
    more = f"\n……共 {len(matches)} 个，只显示前 100 个，请用更精确的模式" if len(matches) > 100 else ""
    return f"找到 {len(matches)} 个文件：\n" + "\n".join(lines) + more


def search_code(pattern: str, path: str = ".", file_glob: str = "", ignore_case: bool = False,
                files_only: bool = False) -> str:
    base = resolve(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"正则表达式有误：{e}。如果要搜普通文本，请对特殊字符转义。"

    targets = [base] if base.is_file() else walk_files(base)
    results, hit_files, total = [], [], 0
    for f in targets:
        if file_glob and not fnmatch.fnmatch(f.name, file_glob):
            continue
        try:
            if f.stat().st_size > 5_000_000 or is_binary(f):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_hit = False
        for ln, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                file_hit = True
                if not files_only and len(results) < SEARCH_MAX_RESULTS:
                    results.append(f"{rel(f)}:{ln}: {line.strip()[:200]}")
        if file_hit:
            hit_files.append(rel(f))

    if not total:
        return f"没有找到匹配 {pattern!r} 的内容"
    if files_only:
        return f"{len(hit_files)} 个文件包含匹配：\n" + "\n".join(hit_files[:200])
    tail = (f"\n……共 {total} 处匹配（{len(hit_files)} 个文件），只显示前 {SEARCH_MAX_RESULTS} 处。"
            "请缩小范围（指定 path / file_glob）或用 files_only=true 先看文件列表。") if total > len(results) else ""
    return "\n".join(results) + tail


# ====================== 联网工具：搜索 / 抓取网页 ======================
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


# 每个搜索引擎实现成一个函数：输入 (关键词, 数量)，返回 [(标题, 网址, 摘要), ...]
# 这样以后想加新的引擎（百度千帆、秘塔、SearXNG 等）只需再写一个同样格式的函数
def _strip_tags(s: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _search_bocha(query: str, n: int):
    """博查 Web Search API：国内直连，返回带长摘要，适合给大模型用"""
    r = requests.post("https://api.bochaai.com/v1/web-search", timeout=30,
                      headers={"Authorization": f"Bearer {BOCHA_KEY}", "Content-Type": "application/json"},
                      json={"query": query, "summary": True, "freshness": "noLimit", "count": n})
    r.raise_for_status()
    data = r.json()
    data = data.get("data") or data            # 返回结构可能带一层 data
    pages = (data.get("webPages") or {}).get("value", [])
    return [(p.get("name", ""), p.get("url", ""), p.get("summary") or p.get("snippet", "")) for p in pages]


def _search_zhipu(query: str, n: int):
    """智谱 Web Search API：国内直连"""
    r = requests.post("https://open.bigmodel.cn/api/paas/v4/web_search", timeout=30,
                      headers={"Authorization": f"Bearer {ZHIPU_KEY}", "Content-Type": "application/json"},
                      json={"search_query": query[:70], "search_engine": "search_std",
                            "search_intent": False, "count": n})
    r.raise_for_status()
    return [(x.get("title", ""), x.get("link", ""), x.get("content", "")) for x in r.json().get("search_result", [])]


def _search_bing_cn(query: str, n: int):
    """必应中国网页搜索：不需要 key，靠解析网页结果，页面改版后可能失效"""
    r = requests.get("https://cn.bing.com/search", params={"q": query}, timeout=20,
                     headers={**UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    r.raise_for_status()
    items = []
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', r.text):
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        sm = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
        items.append((_strip_tags(m.group(2)), m.group(1), _strip_tags(sm.group(1)) if sm else ""))
        if len(items) >= n:
            break
    return items


def _search_tavily(query: str, n: int):
    """Tavily：国外服务，国内网络不一定连得上，仅在配置了 key 时作为备选"""
    r = requests.post("https://api.tavily.com/search", timeout=30,
                      json={"api_key": TAVILY_KEY, "query": query, "max_results": n})
    r.raise_for_status()
    return [(x["title"], x["url"], x.get("content", "")) for x in r.json().get("results", [])]


def web_search(query: str, max_results: int = 5) -> str:
    max_results = max(1, min(int(max_results), 10))
    print(f"  🔍 搜索：{query}")

    # 按优先级排好引擎：配了 key 的优先，必应中国兜底（不用 key）
    engines = []
    if BOCHA_KEY:
        engines.append(("博查", _search_bocha))
    if ZHIPU_KEY:
        engines.append(("智谱", _search_zhipu))
    engines.append(("必应中国", _search_bing_cn))
    if TAVILY_KEY:
        engines.append(("Tavily", _search_tavily))

    errors = []
    for name, fn in engines:           # 一个失败就自动换下一个
        try:
            items = fn(query, max_results)
        except Exception as e:
            errors.append(f"{name}：{type(e).__name__}")
            continue
        if not items:
            errors.append(f"{name}：无结果")
            continue
        print(f"     （来源：{name}，{len(items)} 条）")
        out = [f"[{i}] {t}\n    {u}\n    {c[:300]}" for i, (t, u, c) in enumerate(items, 1)]
        return "\n".join(out) + "\n\n（以上只是摘要，需要详细内容请用 web_fetch 打开对应网址）"

    return ("所有搜索引擎都没有返回结果（" + "；".join(errors) + "）。"
            "可以换个关键词重试；如果多次失败，请告诉用户搜索暂时不可用，不要编造答案。")


def html_to_text(html: str) -> str:
    try:
        import trafilatura   # 能自动去掉导航栏、广告，只留正文
        text = trafilatura.extract(html, include_links=False, include_tables=True)
        if text:
            return text
    except ImportError:
        pass
    html = re.sub(r"(?is)<(script|style|noscript|nav|footer|header).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    for a, b in [("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")]:
        text = text.replace(a, b)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def web_fetch(url: str, start: int = 0) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    print(f"  🌐 打开网页：{url}")
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return f"打开网页失败：{e}"
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        r.encoding = r.apparent_encoding or r.encoding   # 避免中文网页乱码
        text = html_to_text(r.text)
    elif "json" in ctype or "text" in ctype:
        text = r.text
    else:
        return f"该网址是 {ctype} 类型（{len(r.content)} 字节），不是网页。需要的话可以用 run_command 配合 curl 下载到工作目录。"

    start = max(0, int(start))
    chunk = text[start: start + FETCH_MAX_CHARS]
    tail = ""
    if start + FETCH_MAX_CHARS < len(text):
        tail = f"\n\n……全文共 {len(text)} 字符，可用 start={start + FETCH_MAX_CHARS} 继续读取"
    return f"[{url}]\n{chunk}{tail}"


# ====================== 第 3 层：长期记忆 ======================
def load_memory() -> str:
    return MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else ""


def save_memory(content: str) -> str:
    content = content.strip().replace("\n", " ")
    if content in load_memory():
        return "这条记忆已存在，无需重复保存"
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- [{datetime.now():%Y-%m-%d}] {content}\n")
    print(f"  💾 已记住：{content}")
    return "已保存到长期记忆"


def forget_memory(keyword: str) -> str:
    lines = load_memory().splitlines(keepends=True)
    kept = [l for l in lines if keyword not in l]
    MEMORY_FILE.write_text("".join(kept), encoding="utf-8")
    removed = len(lines) - len(kept)
    if removed:
        print(f"  🗑  删除了 {removed} 条包含「{keyword}」的记忆")
    return f"删除了 {removed} 条记忆"


# ====================== 第 1、2 层：会话 ======================
class Session:
    def __init__(self, sid: str | None = None):
        self.id = sid or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.title = ""
        self.summary = ""
        self.messages = []

    @property
    def path(self) -> Path:
        return SESSIONS_DIR / f"{self.id}.json"

    def save(self):
        data = {"id": self.id, "title": self.title, "summary": self.summary,
                "messages": self.messages, "updated": datetime.now().isoformat()}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, sid: str) -> "Session":
        data = json.loads((SESSIONS_DIR / f"{sid}.json").read_text(encoding="utf-8"))
        s = cls(data["id"])
        s.title, s.summary, s.messages = data["title"], data["summary"], data["messages"]
        return s

    @staticmethod
    def list_all():
        items = []
        for p in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
            d = json.loads(p.read_text(encoding="utf-8"))
            items.append((d["id"], d["title"] or "(空会话)", d["updated"][:16]))
        return items


def build_system_prompt(session: Session) -> str:
    memory = load_memory() or "（暂无）"
    summary = session.summary or "（无，这是对话开头）"
    return f"""你是一个能操作电脑、能上网的助手，工作目录是 {WORKSPACE}。
今天是 {datetime.now():%Y-%m-%d}。

## 工作方式
1. 先调查清楚现状，再动手，不要瞎猜。
2. 每做一步，用一句话说明你的发现和下一步打算。
3. 用户的原始资料文件（文档、表格、PPT、图片等）不要直接修改，输出新文件；代码和配置文件可以直接编辑。
4. 完成后要验证结果（运行、测试、重新打开检查），再简洁地总结。
5. 不确定的事实、陌生的库或报错，先搜索再下结论，不要编造。

## 工具使用规范
- 查看文件内容用 read_file（不要用 cat）。大文件按 offset/limit 分段读，只读需要的部分。
- 修改文件用 edit_file（不要用 sed、echo >）：
  · 必须先 read_file；
  · old_str 要从 read_file 的结果中原样复制，**不要带行号和 → 前缀**，缩进必须一致；
  · old_str 要包含足够上下文以保证唯一；多处修改就多次调用。
- 新建文件用 write_file；已有文件只做局部修改时，优先用 edit_file 而不是整个重写。
- 找文件用 find_files，搜文件内容用 search_code（不要用 find/grep 命令）。结果太多时缩小范围。
- run_command 用于运行程序、安装依赖、执行测试、处理二进制文件等其他操作。
- web_search 用于查最新信息、文档、报错解决方案；搜索结果只是摘要，关键信息要用 web_fetch 打开原网页核实。
  回答中引用了网上的信息时，注明来源网址。
- 某个操作被用户拒绝时，不要换个写法强行绕过，应该换方案或询问用户。

## 关于记忆
- 你有长期记忆（见下方），在不同会话之间保留。
- 用户透露了**长期有效**的信息时（偏好、身份、常用路径、项目背景、明确要求“记住”的事），调用 save_memory。
- 不要保存一次性的任务细节、临时结果或敏感信息（密码、密钥）。
- 用户要求忘记某事，或记忆已过时，调用 forget_memory。

## 长期记忆
{memory}

## 本次会话的早期对话摘要
{summary}
"""


def build_messages(session: Session) -> list:
    return [{"role": "system", "content": build_system_prompt(session)}] + session.messages


# ====================== 上下文压缩 ======================
def compress(session: Session) -> bool:
    user_idx = [i for i, m in enumerate(session.messages) if m["role"] == "user"]
    if len(user_idx) <= KEEP_RECENT_TURNS:
        return False
    cut = user_idx[-KEEP_RECENT_TURNS]
    old, recent = session.messages[:cut], session.messages[cut:]

    lines = []
    for m in old:
        if m["role"] == "user":
            lines.append(f"用户：{m['content']}")
        elif m["role"] == "assistant":
            if m.get("content"):
                lines.append(f"助手：{m['content']}")
            for tc in m.get("tool_calls") or []:
                lines.append(f"助手调用工具 {tc['function']['name']}：{tc['function']['arguments'][:300]}")
        elif m["role"] == "tool":
            lines.append(f"工具结果：{m['content'][:500]}")

    prompt = f"""请把下面的对话压缩成一份摘要，供助手继续工作时参考。
要求保留：用户的目标和要求、已完成的事、关键发现和结论、修改过/产生的文件路径、查到的重要网址、未完成的事项。
去掉：冗长的命令输出、重复的尝试过程。用中文分条列出，不超过 400 字。

【之前已有的摘要】
{session.summary or '无'}

【需要压缩的新对话】
{chr(10).join(lines)}"""

    print("  🗜  对话较长，正在压缩早期内容……")
    resp = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
    session.summary = resp.choices[0].message.content
    session.messages = recent
    READ_STATE.clear()   # 压缩后模型已看不到之前读过的原文，要求它重新读再改
    session.save()
    return True


# ====================== 工具定义（给模型看的说明书） ======================
def tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    tool("run_command", "在工作目录执行一条 bash 命令，返回输出。用于运行程序、测试、安装依赖、处理二进制文件等。"
         "不要用它来读文件、改文件或搜索（有专门工具）。危险命令需要用户确认。",
         {"command": {"type": "string"}}, ["command"]),

    tool("read_file", "读取文本文件，返回带行号的内容（格式：行号→内容）。大文件请分段读取。",
         {"path": {"type": "string", "description": "文件路径，相对于工作目录或绝对路径"},
          "offset": {"type": "integer", "description": "从第几行开始，默认 1"},
          "limit": {"type": "integer", "description": f"读多少行，默认 {READ_DEFAULT_LINES}"}},
         ["path"]),

    tool("edit_file", "精确替换文件中的一段文本。必须先 read_file。old_str 必须与文件内容逐字一致且唯一，"
         "不能包含行号前缀。找不到或不唯一时会报错并给出提示。",
         {"path": {"type": "string"},
          "old_str": {"type": "string", "description": "要被替换的原文（含足够上下文以保证唯一）"},
          "new_str": {"type": "string", "description": "替换后的新内容"},
          "replace_all": {"type": "boolean", "description": "是否替换所有出现的地方，默认 false"}},
         ["path", "old_str", "new_str"]),

    tool("write_file", "新建文件，或整体覆盖已有文件（覆盖前必须先 read_file）。局部修改请用 edit_file。",
         {"path": {"type": "string"}, "content": {"type": "string", "description": "完整的文件内容"}},
         ["path", "content"]),

    tool("find_files", "按通配符查找文件，如 '*.py'、'src/**/*.ts'、'*report*'。按修改时间排序，最多返回 100 个。",
         {"pattern": {"type": "string"}, "path": {"type": "string", "description": "在哪个目录下找，默认工作目录"}},
         ["pattern"]),

    tool("search_code", "在文件内容中按正则表达式搜索，返回 文件:行号: 内容。会跳过二进制文件和 .git/node_modules 等目录。",
         {"pattern": {"type": "string", "description": "正则表达式"},
          "path": {"type": "string", "description": "搜索的目录或文件，默认工作目录"},
          "file_glob": {"type": "string", "description": "只搜某类文件，如 '*.py'"},
          "ignore_case": {"type": "boolean"},
          "files_only": {"type": "boolean", "description": "只返回包含匹配的文件名列表"}},
         ["pattern"]),

    tool("web_search", "联网搜索，返回标题、网址和摘要。用于查询最新信息、技术文档、报错解决办法等。",
         {"query": {"type": "string", "description": "搜索关键词，简洁具体；中文问题用中文搜，国外技术文档可再换英文关键词"},
          "max_results": {"type": "integer", "description": "结果数量，默认 5，最多 10"}},
         ["query"]),

    tool("web_fetch", "打开网页并提取正文文本。长网页分段返回。",
         {"url": {"type": "string"},
          "start": {"type": "integer", "description": "从第几个字符开始读，用于读取长网页的后续部分，默认 0"}},
         ["url"]),

    tool("save_memory", "把一条长期有效的信息存入长期记忆，以后的会话也能看到",
         {"content": {"type": "string", "description": "一句话描述要记住的事实"}}, ["content"]),

    tool("forget_memory", "删除长期记忆中包含某个关键词的条目",
         {"keyword": {"type": "string"}}, ["keyword"]),
]


# ====================== 工具执行 ======================
def run_command(command: str) -> str:
    if (err := check_command(command)):
        return err
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300, cwd=WORKSPACE)
        out = (r.stdout + r.stderr).strip() or "(无输出)"
        if r.returncode != 0:
            out = f"[退出码 {r.returncode}]\n" + out
        if len(out) > TOOL_OUTPUT_LIMIT:
            out = out[:2000] + f"\n...（省略 {len(out) - TOOL_OUTPUT_LIMIT} 字符）...\n" + out[-6000:]
        return out
    except subprocess.TimeoutExpired:
        return "命令超时（300 秒）"
    except Exception as e:
        return f"出错: {e}"


HANDLERS = {
    "run_command": run_command, "read_file": read_file, "edit_file": edit_file, "write_file": write_file,
    "find_files": find_files, "search_code": search_code, "web_search": web_search, "web_fetch": web_fetch,
    "save_memory": save_memory, "forget_memory": forget_memory,
}


def execute_tool(call) -> str:
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return "参数不是合法 JSON，请重试"
    fn = HANDLERS.get(name)
    if not fn:
        return f"未知工具 {name}，可用工具：{', '.join(HANDLERS)}"
    # 终端显示：让用户看到 Agent 在干什么
    if name == "run_command":
        print(f"  ▶ 运行命令: {args.get('command')}")
    elif name == "read_file":
        print(f"  📖 读取 {args.get('path')}" + (f" (从第 {args['offset']} 行)" if args.get("offset") else ""))
    elif name in ("find_files", "search_code"):
        print(f"  🔎 {name}: {args.get('pattern')}" + (f" in {args['path']}" if args.get("path") else ""))
    try:
        return fn(**args)
    except TypeError as e:
        return f"参数错误：{e}"
    except Exception as e:
        return f"工具执行出错：{type(e).__name__}: {e}"


def assistant_to_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]
    return d


def fix_dangling_tool_calls(session: Session):
    for i in range(len(session.messages) - 1, -1, -1):
        m = session.messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in session.messages[i + 1:] if x["role"] == "tool"}
            for tc in m["tool_calls"]:
                if tc["id"] not in answered:
                    session.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "（用户中断，未执行）"})
            return


# ====================== Agent 循环 ======================
def run_turn(session: Session, user_input: str):
    session.messages.append({"role": "user", "content": user_input})
    if not session.title:
        session.title = user_input[:30]
    try:
        for _ in range(MAX_STEPS):
            resp = client.chat.completions.create(model=MODEL, messages=build_messages(session), tools=TOOLS)
            msg = resp.choices[0].message
            session.messages.append(assistant_to_dict(msg))
            if msg.content:
                print(f"\n🤖 {msg.content}")
            if not msg.tool_calls:
                break
            for call in msg.tool_calls:
                result = execute_tool(call)
                session.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            session.save()
            if resp.usage and resp.usage.prompt_tokens > CONTEXT_LIMIT:
                compress(session)
        else:
            print(f"\n⚠️  已达到最大步数 {MAX_STEPS}，任务暂停。输入「继续」可让它接着做。")
    except KeyboardInterrupt:
        print("\n⏸  已中断当前任务（会话保留，可继续对话）")
        fix_dangling_tool_calls(session)
    except Exception as e:
        print(f"\n❌ 调用模型出错：{e}")
        fix_dangling_tool_calls(session)
    session.save()


# ====================== 命令行交互 ======================
HELP = """可用命令：
  /new            开始新会话（长期记忆仍然保留）
  /sessions       列出历史会话
  /resume <id>    恢复某个历史会话
  /memory         查看长期记忆
  /forget <词>    删除包含该词的长期记忆
  /compact        手动压缩当前会话
  /allowed        查看本会话已“一直允许”的危险操作类别
  /status         查看当前会话状态
  /help           显示帮助
  /exit           退出（Ctrl+D 也可以）
运行中按 Ctrl+C 可以中断当前任务。"""


def new_session_state():
    READ_STATE.clear()
    ALWAYS_ALLOWED.clear()


def main():
    session = Session()
    mem_count = len([l for l in load_memory().splitlines() if l.strip()])
    search_engine = "、".join([n for n, k in (("博查", BOCHA_KEY), ("智谱", ZHIPU_KEY)) if k] + ["必应中国"])
    print(f"🤖 Mini Agent v2 | 模型 {MODEL} | 搜索 {search_engine} | 长期记忆 {mem_count} 条 | /help 查看命令\n")

    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not text:
            continue

        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            arg = arg.strip()
            if cmd == "/exit":
                print("再见！")
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/new":
                session = Session()
                new_session_state()
                print("✨ 已开始新会话")
            elif cmd == "/sessions":
                for sid, title, updated in Session.list_all()[:20]:
                    mark = " ← 当前" if sid == session.id else ""
                    print(f"  {sid}  {updated}  {title}{mark}")
            elif cmd == "/resume":
                try:
                    session = Session.load(arg)
                    new_session_state()
                    print(f"📂 已恢复会话：{session.title}（{len(session.messages)} 条消息）")
                except FileNotFoundError:
                    print("找不到该会话，用 /sessions 查看 id")
            elif cmd == "/memory":
                print(load_memory() or "（长期记忆为空）")
            elif cmd == "/forget":
                forget_memory(arg) if arg else print("用法：/forget 关键词")
            elif cmd == "/compact":
                print("✅ 已压缩" if compress(session) else "对话还不够长，无需压缩")
            elif cmd == "/allowed":
                print("、".join(ALWAYS_ALLOWED) or "（无，所有危险操作都会询问）")
            elif cmd == "/status":
                print(f"  会话 {session.id} | 消息 {len(session.messages)} 条 | "
                      f"有摘要：{'是' if session.summary else '否'} | 已读文件 {len(READ_STATE)} 个")
            else:
                print("未知命令，输入 /help 查看")
            continue

        run_turn(session, text)
        print()


if __name__ == "__main__":
    main()