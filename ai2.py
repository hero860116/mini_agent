"""
mini_agent.py —— 支持命令行交互 + 分层记忆的迷你 Agent

记忆分三层：
  1. 工作记忆：当前会话的完整对话（messages），每轮都发给模型
  2. 会话记忆：对话太长时，把旧对话压缩成摘要；会话保存到磁盘，可随时恢复
  3. 长期记忆：跨会话的 memory.md，由模型通过工具自己写入，每次启动都加载

运行：
  pip install openai
  export LLM_API_KEY=你的key
  export LLM_BASE_URL=https://api.deepseek.com      # 通义：https://dashscope.aliyuncs.com/compatible-mode/v1
  export LLM_MODEL=deepseek-chat                    # 通义：qwen-plus
  python mini_agent.py
"""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from openai import OpenAI

try:
    import readline  # 让输入支持 ↑↓ 翻历史、左右移动光标（Windows 下没有也不影响）
except ImportError:
    pass

# ====================== 配置 ======================
API_KEY = os.getenv("LLM_API_KEY", "sk-c0f724d19069479d8572f5ff5245ca34")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")

WORKSPACE = Path("./workspace")
DATA_DIR = Path("./.agent")
SESSIONS_DIR = DATA_DIR / "sessions"
MEMORY_FILE = DATA_DIR / "memory.md"
for d in (WORKSPACE, SESSIONS_DIR):
    d.mkdir(parents=True, exist_ok=True)

MAX_STEPS = 40            # 单个任务最多循环几圈，防止死循环
CONTEXT_LIMIT = 50_000    # 发给模型的 token 超过这个数就触发压缩
KEEP_RECENT_TURNS = 3     # 压缩时保留最近几轮完整对话
TOOL_OUTPUT_LIMIT = 8000  # 单次工具输出最多保留多少字符
DANGEROUS = ["rm ", "rm -", "mkfs", "dd ", "shutdown", "reboot", "> /dev", "chmod -R", ":(){"]

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


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


# ====================== 第 1、2 层：会话（工作记忆 + 摘要） ======================
class Session:
    def __init__(self, sid: str | None = None):
        self.id = sid or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.title = ""
        self.summary = ""     # 被压缩掉的旧对话的摘要
        self.messages = []    # 最近的完整对话（不含 system）

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
    """每次调用模型都会重新生成 system prompt，把长期记忆和会话摘要注入进去"""
    memory = load_memory() or "（暂无）"
    summary = session.summary or "（无，这是对话开头）"
    return f"""你是一个能操作电脑的助手，工作目录是 {WORKSPACE.resolve()}。
今天是 {datetime.now():%Y-%m-%d}。

## 工作方式
1. 先调查清楚现状，再动手，不要瞎猜。
2. 每做一步，用一句话说明你的发现和下一步打算。
3. 不要修改原文件，输出新文件。
4. 完成后要验证结果，再简洁地总结。

## 关于记忆
- 你有长期记忆（见下方）。它在不同会话之间保留。
- 用户透露了**长期有效**的信息时（偏好、身份、常用路径、项目背景、明确要求"记住"的事），调用 save_memory 保存。
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
    """把较早的对话压缩成摘要，只保留最近 KEEP_RECENT_TURNS 轮完整对话"""
    user_idx = [i for i, m in enumerate(session.messages) if m["role"] == "user"]
    if len(user_idx) <= KEEP_RECENT_TURNS:
        return False
    cut = user_idx[-KEEP_RECENT_TURNS]  # 只在 user 消息处切，保证工具调用和结果不被拆开
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
要求保留：用户的目标和要求、已完成的事、关键发现和结论、产生的文件路径、未完成的事项。
去掉：冗长的命令输出、重复的尝试过程。用中文分条列出，不超过 400 字。

【之前已有的摘要】
{session.summary or '无'}

【需要压缩的新对话】
{chr(10).join(lines)}"""

    print("  🗜  对话较长，正在压缩早期内容……")
    resp = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}])
    session.summary = resp.choices[0].message.content
    session.messages = recent
    session.save()
    return True


# ====================== 工具 ======================
TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "在工作目录执行一条 bash 命令（可用 python、unzip、zip 等），返回输出",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string", "description": "要执行的命令"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "把一条长期有效的信息存入长期记忆，以后的会话也能看到",
        "parameters": {"type": "object",
                       "properties": {"content": {"type": "string", "description": "一句话描述要记住的事实"}},
                       "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "forget_memory",
        "description": "删除长期记忆中包含某个关键词的条目",
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"}},
                       "required": ["keyword"]}}},
]


def run_command(command: str) -> str:
    if any(d in command for d in DANGEROUS):
        ok = input(f"  ⚠️  模型想执行危险命令：{command}\n  允许吗？(y/N) ").strip().lower()
        if ok != "y":
            return "用户拒绝执行该命令，请换一种安全的方式"
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=300, cwd=WORKSPACE)
        out = (r.stdout + r.stderr).strip() or "(无输出)"
        if len(out) > TOOL_OUTPUT_LIMIT:
            out = out[:2000] + f"\n...（省略 {len(out) - TOOL_OUTPUT_LIMIT} 字符）...\n" + out[-6000:]
        return out
    except subprocess.TimeoutExpired:
        return "命令超时（300 秒）"
    except Exception as e:
        return f"出错: {e}"


def execute_tool(call) -> str:
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return "参数不是合法 JSON，请重试"
    if name == "run_command":
        print(f"  ▶ 运行命令: {args.get('command')}")
        return run_command(args.get("command", ""))
    if name == "save_memory":
        return save_memory(args.get("content", ""))
    if name == "forget_memory":
        return forget_memory(args.get("keyword", ""))
    return f"未知工具 {name}"


def assistant_to_dict(msg) -> dict:
    """把 SDK 返回的对象转成普通 dict，才能存成 JSON"""
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]
    return d


def fix_dangling_tool_calls(session: Session):
    """用户 Ctrl+C 中断时，可能有工具调用还没有结果，API 会因此报错，这里补上占位结果"""
    for i in range(len(session.messages) - 1, -1, -1):
        m = session.messages[i]
        if m["role"] == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in session.messages[i + 1:] if x["role"] == "tool"}
            for tc in m["tool_calls"]:
                if tc["id"] not in answered:
                    session.messages.append({"role": "tool", "tool_call_id": tc["id"],
                                             "content": "（用户中断，未执行）"})
            return


# ====================== Agent 循环（一轮用户输入） ======================
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
            session.save()  # 每一步都存盘，程序崩溃也不丢
            if resp.usage and resp.usage.prompt_tokens > CONTEXT_LIMIT:
                compress(session)
        else:
            print(f"\n⚠️  已达到最大步数 {MAX_STEPS}，任务暂停。输入「继续」可让它接着做。")
    except KeyboardInterrupt:
        print("\n⏸  已中断当前任务（会话保留，可继续对话）")
        fix_dangling_tool_calls(session)
    except Exception as e:
        print(f"\n❌ 调用模型出错：{e}")
    session.save()


# ====================== 命令行交互 ======================
HELP = """可用命令：
  /new            开始新会话（长期记忆仍然保留）
  /sessions       列出历史会话
  /resume <id>    恢复某个历史会话
  /memory         查看长期记忆
  /forget <词>    删除包含该词的长期记忆
  /compact        手动压缩当前会话
  /status         查看当前会话状态
  /help           显示帮助
  /exit           退出（Ctrl+D 也可以）
运行中按 Ctrl+C 可以中断当前任务。"""


def main():
    session = Session()
    mem_count = len([l for l in load_memory().splitlines() if l.strip()])
    print(f"🤖 Mini Agent 已启动 | 模型 {MODEL} | 长期记忆 {mem_count} 条 | 输入 /help 查看命令\n")

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
                print("✨ 已开始新会话")
            elif cmd == "/sessions":
                for sid, title, updated in Session.list_all()[:20]:
                    mark = " ← 当前" if sid == session.id else ""
                    print(f"  {sid}  {updated}  {title}{mark}")
            elif cmd == "/resume":
                try:
                    session = Session.load(arg)
                    print(f"📂 已恢复会话：{session.title}（{len(session.messages)} 条消息）")
                except FileNotFoundError:
                    print("找不到该会话，用 /sessions 查看 id")
            elif cmd == "/memory":
                print(load_memory() or "（长期记忆为空）")
            elif cmd == "/forget":
                forget_memory(arg) if arg else print("用法：/forget 关键词")
            elif cmd == "/compact":
                print("✅ 已压缩" if compress(session) else "对话还不够长，无需压缩")
            elif cmd == "/status":
                print(f"  会话 {session.id} | 消息 {len(session.messages)} 条 | "
                      f"有摘要：{'是' if session.summary else '否'}")
            else:
                print("未知命令，输入 /help 查看")
            continue

        run_turn(session, text)
        print()


if __name__ == "__main__":
    main()