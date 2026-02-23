import gradio as gr
import requests
import json
import time
from sseclient import SSEClient
import os
from loguru import logger

# 后端API地址
FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "http://127.0.0.1:5001")
FASTAPI_API_PREFIX = os.getenv("FASTAPI_API_PREFIX", "/api/v1")


def _api_url(path: str) -> str:
    return f"{FASTAPI_BASE_URL.rstrip('/')}{FASTAPI_API_PREFIX}{path}"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {user_session.token}"}


def _recent_messages_to_chatbot(messages: list[dict]) -> list[dict]:
    rendered: list[dict] = []
    for msg in messages:
        role = str(msg.get("role", ""))
        author = str(msg.get("author", ""))
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        if role == "player":
            rendered.append({"role": "user", "content": content})
        else:
            prefix = f"{author}: " if author else ""
            rendered.append({"role": "assistant", "content": f"{prefix}{content}"})
    return rendered


def _build_start_history(start_payload: dict) -> list[dict]:
    history: list[dict] = []
    system_message = str(start_payload.get("system_message", "")).strip()
    opening = str(start_payload.get("opening", "")).strip()
    recent_messages = start_payload.get("recent_messages", []) or []

    if system_message:
        history.append({"role": "assistant", "content": system_message})
    if opening:
        history.append({"role": "assistant", "content": f"Narrator: {opening}"})
    history.extend(_recent_messages_to_chatbot(recent_messages))
    return history

# 全局状态管理
class UserSession:
    def __init__(self):
        self.logged_in = False
        self.token = None
        self.username = None
        self.chat_history = []
    
    def login(self, username, token):
        self.logged_in = True
        self.username = username
        self.token = token
        logger.info(f"用户 {username} 登录成功")
    
    def logout(self):
        self.logged_in = False
        self.token = None
        self.username = None
        self.chat_history = []
        logger.info("用户已登出")
    
    def add_chat(self, query, response):
        self.chat_history.append({"query": query, "response": response})

# 创建用户会话实例
user_session = UserSession()

# ===================================================================
# 登录和认证函数
# ===================================================================

def login(username, password):
    """处理用户登录"""
    if not username or not password:
        return (
            "未登录",
            "用户名和密码不能为空",
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(interactive=True),
            gr.update(visible=True, interactive=False),
            [],
        )
    
    try:
        response = requests.post(
            _api_url("/User/login"),
            json={"username": username, "password": password}
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                # 更新用户会话
                token = data["data"]["token"]
                user_session.login(username, token)

                try:
                    options_resp = requests.get(
                        _api_url("/game/start/options"),
                        headers=_auth_headers(),
                        timeout=15,
                    )
                    if options_resp.status_code != 200:
                        raise RuntimeError(f"获取开局选项失败: HTTP {options_resp.status_code}")
                    options = options_resp.json()
                except Exception as e:
                    logger.error(f"获取开局选项异常: {str(e)}")
                    return (
                        f"已登录: {username}",
                        f"登录成功，但初始化失败: {str(e)}",
                        gr.update(visible=False),
                        gr.update(visible=False),
                        gr.update(visible=False),
                        gr.update(value=""),
                        gr.update(value=""),
                        gr.update(interactive=True),
                        gr.update(visible=True, interactive=False),
                        [],
                    )

                has_save = bool(options.get("has_save"))
                prompt = str(options.get("prompt", "")).strip()
                preview_messages = options.get("recent_messages", []) if has_save else []
                preview_lines = []
                for msg in preview_messages:
                    author = str(msg.get("author", ""))
                    content = str(msg.get("content", "")).strip()
                    if not content:
                        continue
                    preview_lines.append(f"{author}: {content}")
                preview_text = "\n\n".join(preview_lines) if preview_lines else "暂无历史回顾。"

                return (
                    f"已登录: {username}",
                    f"登录成功！欢迎 {username}，请选择开局方式。",
                    gr.update(visible=False),
                    gr.update(visible=True),
                    gr.update(visible=False),
                    gr.update(value=prompt or "请选择开局方式"),
                    gr.update(value=preview_text),
                    gr.update(interactive=True),
                    gr.update(visible=True, interactive=has_save),
                    [],
                )
            else:
                return (
                    "未登录",
                    f"登录失败: {data.get('message', '未知错误')}",
                    gr.update(visible=True),
                    gr.update(visible=False),
                    gr.update(visible=True),
                    gr.update(value=""),
                    gr.update(value=""),
                    gr.update(interactive=True),
                    gr.update(visible=True, interactive=False),
                    [],
                )
        else:
            return (
                "未登录",
                f"请求失败: HTTP {response.status_code}",
                gr.update(visible=True),
                gr.update(visible=False),
                gr.update(visible=False),
                gr.update(value=""),
                gr.update(value=""),
                gr.update(interactive=True),
                gr.update(visible=True, interactive=False),
                [],
            )
    except Exception as e:
        logger.error(f"登录异常: {str(e)}")
        return (
            "未登录",
            f"登录异常: {str(e)}",
            gr.update(visible=True),
            gr.update(visible=False),
            gr.update(visible=False),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(interactive=True),
            gr.update(visible=True, interactive=False),
            [],
        )

def logout():
    """处理用户登出"""
    user_session.logout()
    return (
        "已登出",
        gr.update(visible=True),
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(value=""),
        gr.update(value=""),
        gr.update(interactive=True),
        gr.update(visible=True, interactive=False),
        [],
    )


def choose_start_mode(action: str):
    """处理开局模式选择：new/continue"""
    if not user_session.logged_in:
        return (
            "请先登录后再选择开局方式。",
            gr.update(visible=True),
            gr.update(visible=False),
            [],
            gr.update(value=""),
        )

    try:
        response = requests.post(
            _api_url("/game/start"),
            headers=_auth_headers(),
            json={"action": action},
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(f"开局失败: HTTP {response.status_code}")

        payload = response.json()
        history = _build_start_history(payload)
        user_session.chat_history = history
        msg = "已进入会话。"
        return (
            msg,
            gr.update(visible=False),
            gr.update(visible=True),
            history,
            gr.update(value=""),
        )
    except Exception as e:
        logger.error(f"开局选择异常: {str(e)}")
        return (
            f"开局失败: {str(e)}",
            gr.update(visible=True),
            gr.update(visible=False),
            [],
            gr.update(value=""),
        )

# ===================================================================
# 流式聊天函数
# ===================================================================
def stream_chat(query, history):
    """处理流式聊天查询"""
    history = history or []

    if not user_session.logged_in:
        history.append({"role": "user", "content": query})
        history.append({"role": "assistant", "content": "请先登录！"})
        yield history
        return
    
    # 准备API请求
    headers = {
        "Authorization": f"Bearer {user_session.token}",
        "Accept": "text/event-stream"
    }
    params = {"query": query}
    
    # 添加用户和助手占位消息到历史（Gradio messages 格式）
    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": "思考中..."})
    # 先回传一次，让前端立即有反馈
    yield list(history)
    
    try:
        # 创建SSE客户端
        messages = SSEClient(_api_url("/chat/sse"), 
                           params=params, 
                           headers=headers)
        
        response_text = ""
        has_started_streaming = False
        for msg in messages:
            # 检查流是否已结束
            if msg is None:
                logger.debug("收到空消息，流结束")
                break
                
            # 打印原始消息用于调试
            logger.debug(f"原始消息: {msg.data}")
            
            try:
                # 解析JSON数据
                data = json.loads(msg.data)
                
                # 检查是否为结束标志
                if data.get("type") == "stream_end":
                    logger.debug("收到流结束标志，停止接收")
                    break
                
                # 处理text_delta类型的消息
                if data.get("type") == "text_delta":
                    content = data.get("delta", {}).get("content", "")
                    
                    # 处理Unicode转义字符
                    if isinstance(content, str) and any(c in content for c in ["\\u", "\\U"]):
                        try:
                            content = content.encode('utf-8').decode('unicode_escape')
                        except:
                            pass
                    
                    response_text += content
                    has_started_streaming = True
                    
                    # 更新最后一条助手回复
                    history[-1]["content"] = response_text
                    yield list(history)
                    
            except json.JSONDecodeError:
                # 处理非JSON响应
                if msg.data == "[DONE]":
                    logger.debug("收到DONE标志，流结束")
                    break
                logger.warning(f"无法解析的消息: {msg.data}")
                continue
            except Exception as e:
                logger.error(f"处理消息时出错: {str(e)}")
                continue
                
            # 添加延迟使UI更新更平滑
            time.sleep(0.02)
        
        # 保存到聊天历史
        final_text = response_text if has_started_streaming else "（未收到响应内容）"
        history[-1]["content"] = final_text
        yield list(history)
        user_session.add_chat(query, final_text)
        
    except Exception as e:
        logger.error(f"聊天请求出错: {str(e)}")
        error_msg = f"聊天请求出错: {str(e)}"
        if history and history[-1].get("role") == "assistant":
            history[-1]["content"] = error_msg
        else:
            history.append({"role": "assistant", "content": error_msg})
        yield list(history)
# ===================================================================
# Gradio UI 界面定义
# ===================================================================

with gr.Blocks(title="RAG 智能问答系统") as chat_demo_ui:
    gr.Markdown("# 🧠 RAG 智能问答系统")
    
    # 登录状态显示
    with gr.Row():
        status_display = gr.Textbox(label="登录状态", value="未登录", interactive=False)
    
    # 登录界面
    with gr.Column(visible=True) as login_section:
        with gr.Row():
            username = gr.Textbox(label="用户名", placeholder="输入用户名")
            password = gr.Textbox(label="密码", placeholder="输入密码", type="password")
        
        login_btn = gr.Button("登录", variant="primary")
        login_result = gr.Textbox(label="登录结果", interactive=False)
    
    # 开局选择界面
    with gr.Column(visible=False, elem_id="start-section") as start_section:
        start_prompt = gr.Markdown("请选择开局方式")
        start_preview = gr.Textbox(
            label="最近对话回顾",
            lines=8,
            interactive=False,
        )
        with gr.Row():
            start_new_btn = gr.Button("新开局", variant="primary", elem_id="start-new-btn")
            start_continue_btn = gr.Button("继续上次游戏", elem_id="start-continue-btn")

    # 主功能界面
    with gr.Column(visible=False) as main_section:
        with gr.Tabs():
            # --- 智能问答 ---
            with gr.TabItem("智能问答"):
                chatbot = gr.Chatbot(label="对话历史", height=400, elem_id="chatbot-history")
                msg = gr.Textbox(
                    label="输入问题",
                    placeholder="在此输入您的问题...",
                    elem_id="chat-input",
                )
                
                with gr.Row():
                    submit_btn = gr.Button("发送", variant="primary")
                    logout_btn = gr.Button("登出")

    
    # ===================================================================
    # 事件处理
    # ===================================================================
    
    # 登录/登出事件
    login_btn.click(
        login,
        inputs=[username, password],
        outputs=[
            status_display,
            login_result,
            login_section,
            start_section,
            main_section,
            start_prompt,
            start_preview,
            start_new_btn,
            start_continue_btn,
            chatbot,
        ],
    )
    
    logout_btn.click(
        logout,
        outputs=[
            status_display,
            login_section,
            start_section,
            main_section,
            start_prompt,
            start_preview,
            start_new_btn,
            start_continue_btn,
            chatbot,
        ],
    )

    start_new_btn.click(
        lambda: choose_start_mode("new"),
        outputs=[login_result, start_section, main_section, chatbot, msg],
    )
    start_continue_btn.click(
        lambda: choose_start_mode("continue"),
        outputs=[login_result, start_section, main_section, chatbot, msg],
    )
    
    # 聊天事件
    msg.submit(
        stream_chat,
        inputs=[msg, chatbot],
        outputs=[chatbot],
        show_progress="hidden"
    )
    
    submit_btn.click(
        stream_chat,
        inputs=[msg, chatbot],
        outputs=[chatbot],
        show_progress="hidden"
    )
    
    gr.HTML(
        """
<script>
(() => {
  const byId = (id) => document.getElementById(id);
  const focusStartButton = () => {
    const continueBtn = byId("start-continue-btn");
    const newBtn = byId("start-new-btn");
    const target = (continueBtn && !continueBtn.disabled) ? continueBtn : newBtn;
    if (target) target.focus();
  };
  const focusChatInput = () => {
    const input = byId("chat-input");
    if (input) input.focus();
  };
  const scrollChatBottom = () => {
    const chat = byId("chatbot-history");
    if (!chat) return;
    chat.scrollTop = chat.scrollHeight;
    const scroller = chat.querySelector("[class*='overflow'], .overflow-y-auto, .wrap");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  };

  const startSection = byId("start-section");
  if (startSection) {
    const observer = new MutationObserver(() => {
      const visible = startSection.offsetParent !== null;
      if (visible) setTimeout(focusStartButton, 60);
    });
    observer.observe(startSection, {attributes: true, attributeFilter: ["class", "style"]});
  }

  const newBtn = byId("start-new-btn");
  const continueBtn = byId("start-continue-btn");
  const onEnterChat = () => {
    setTimeout(scrollChatBottom, 80);
    setTimeout(focusChatInput, 120);
  };
  if (newBtn) newBtn.addEventListener("click", onEnterChat);
  if (continueBtn) continueBtn.addEventListener("click", onEnterChat);

  const chat = byId("chatbot-history");
  if (chat) {
    const chatObserver = new MutationObserver(() => setTimeout(scrollChatBottom, 30));
    chatObserver.observe(chat, {childList: true, subtree: true, characterData: true});
  }
})();
</script>
        """
    )

    # 状态更新
    chat_demo_ui.load(
        fn=lambda: f"已登录: {user_session.username}" if user_session.logged_in else "未登录",
        outputs=[status_display]
    )

# 启动Gradio应用
if __name__ == "__main__":
    chat_demo_ui.queue(default_concurrency_limit=5).launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        theme=gr.themes.Soft(),
    )
