import json, subprocess
from openai import OpenAI   # 兼容 OpenAI 接口的模型都行：DeepSeek、通义、Kimi 等

client = OpenAI(api_key="sk-c0f724d19069479d8572f5ff5245ca34", base_url="https://api.deepseek.com")
MODEL = "deepseek-flash"

# ① 系统提示词：告诉模型它是谁、该怎么干活
SYSTEM_PROMPT = """你是一个能操作电脑的助手，工作目录是 ./workspace。
你可以通过 run_command 工具执行 bash 命令（可使用 python、unzip、zip 等）。
工作方式：
1. 先调查清楚现状，再动手，不要瞎猜。
2. 每做一步，用一句话说明你的发现和下一步打算。
3. 不要修改原文件，输出新文件。
4. 完成后要验证结果（比如检查新文件能否正常打开、大小对比），再总结。"""

# ② 工具说明书：模型靠它知道有哪些工具、怎么调用
TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "在终端执行一条 bash 命令，返回输出",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"],
        },
    },
}]

# ③ 工具的真正实现
def run_command(command):
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, timeout=300, cwd="./workspace")
        out = r.stdout + r.stderr
        return out[-8000:] or "(无输出)"      # 截断，避免撑爆上下文
    except Exception as e:
        return f"出错: {e}"

# ④ Agent 循环
def agent(task, max_steps=40):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    for step in range(max_steps):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        msg = resp.choices[0].message
        messages.append(msg)

        if msg.content:
            print("🤖", msg.content)          # 截图里的那些“我发现……”
        if not msg.tool_calls:                # 没有要调用的工具 = 任务完成
            break
        for call in msg.tool_calls:
            cmd = json.loads(call.function.arguments)["command"]
            print("▶ 运行命令:", cmd)
            result = run_command(cmd)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

#agent("/Users/liweilin/Downloads/workspace/TDEngine进展汇报.pptx有点大，保持内容不变，压缩下大小")
agent("分析下/Users/liweilin/Downloads文件下大小， 哪些内容释放")