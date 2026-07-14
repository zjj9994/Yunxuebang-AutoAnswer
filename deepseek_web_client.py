"""
DeepSeek 网页版客户端
通过 Playwright 自动化操作 chat.deepseek.com 网页版获取答案
无需 API Key，直接使用网页版对话
"""

import re
import time
import logging
from typing import List, Optional

from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from config import DeepSeekWebConfig

logger = logging.getLogger(__name__)


class DeepSeekWebClient:
    """
    DeepSeek 网页版客户端

    通过 Playwright 驱动浏览器访问 chat.deepseek.com，
    自动发送题目并提取回复，无需 API Key。

    支持两种使用方式：
    1. 共享浏览器：由 WebAutomator 传入已有的 Playwright browser，DeepSeek 在新标签页打开
    2. 独立浏览器：自行创建浏览器实例（适用于 Android 模式，电脑端跑 DeepSeek）
    """

    DEEPSEEK_URL = "https://chat.deepseek.com/"

    # 输入框选择器（多策略）
    INPUT_SELECTORS = [
        "textarea[placeholder*='DeepSeek']",
        "textarea[placeholder*='发送消息']",
        "textarea[placeholder*='Send a message']",
        "textarea[placeholder*='给 DeepSeek']",
        "#chat-input",
        "div[contenteditable='true']",
        "textarea",
    ]

    # 发送按钮选择器
    SEND_SELECTORS = [
        "div[role='button'] svg",  # 常见发送图标
        "button[type='submit']",
        "button[class*='send']",
        "div[class*='send-button']",
        ".ds-icon-button",
    ]

    def __init__(self, config: DeepSeekWebConfig):
        self.config = config
        self.playwright = None
        self.browser = None  # 可外部传入
        self._owns_browser = False  # 标记是否自建浏览器
        self.page = None
        self._last_response_count = 0  # 用于追踪新回复

    async def init_with_shared_browser(self, playwright, browser):
        """
        使用共享浏览器（Web 模式）
        在已有 browser 中新开一个标签页访问 DeepSeek
        """
        self.playwright = playwright
        self.browser = browser
        self._owns_browser = False
        self.page = await browser.new_page()
        await self._setup_page()
        logger.info("DeepSeek 客户端已初始化（共享浏览器模式）")

    async def init_standalone(self):
        """
        独立启动浏览器（Android 模式）
        自行创建 Playwright 浏览器实例
        """
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.config.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._owns_browser = True
        self.page = await self.browser.new_page()
        await self._setup_page()
        logger.info("DeepSeek 客户端已初始化（独立浏览器模式）")

    async def _setup_page(self):
        """页面初始化设置"""
        # 反自动化检测
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        # 设置视口
        await self.page.set_viewport_size({
            "width": self.config.viewport_width,
            "height": self.config.viewport_height,
        })

    async def navigate_and_login(self):
        """导航到 DeepSeek 并等待用户登录"""
        await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=30000)
        logger.info(f"已打开 DeepSeek: {self.DEEPSEEK_URL}")

        # 检查是否需要登录
        await self.page.wait_for_timeout(2000)

        # 等待输入框出现（说明已登录）
        input_found = await self._find_input_element()
        if not input_found:
            logger.info("DeepSeek 需要登录，请在浏览器中完成登录")
            await self._notify_and_wait(
                "请在 DeepSeek 页面完成登录（支持手机号/邮箱/微信扫码登录）"
            )
            # 登录后再次检查
            input_found = await self._find_input_element()
            if not input_found:
                logger.error("登录后仍未找到输入框，请检查页面是否正常加载")
                return False

        logger.info("DeepSeek 已就绪，可以开始答题")
        return True

    async def _notify_and_wait(self, message: str):
        """在页面上显示提示并等待用户在终端按回车"""
        try:
            await self.page.evaluate(f"""
                () => {{
                    const div = document.createElement('div');
                    div.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);'
                        + 'background:#4CAF50;color:white;padding:15px 30px;border-radius:8px;'
                        + 'font-size:16px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
                    div.textContent = '{message}';
                    document.body.appendChild(div);
                }}
            """)
        except Exception:
            pass
        input(f"\n>>> {message}，完成后按回车继续...")

    async def _find_input_element(self):
        """查找输入框元素，返回元素或 None"""
        for selector in self.INPUT_SELECTORS:
            try:
                elem = self.page.locator(selector).first
                if await elem.is_visible(timeout=2000):
                    logger.debug(f"找到输入框: {selector}")
                    return elem
            except Exception:
                continue
        return None

    async def _find_send_button(self):
        """查找发送按钮，返回元素或 None"""
        for selector in self.SEND_SELECTORS:
            try:
                elem = self.page.locator(selector).first
                if await elem.is_visible(timeout=2000):
                    return elem
            except Exception:
                continue

        # 回退：查找输入框附近的可点击按钮
        try:
            buttons = self.page.locator("button, div[role='button']").all()
            for btn in await buttons:
                text = (await btn.inner_text()).strip() if await btn.is_visible() else ""
                if text in ("发送", "Send", ""):
                    # 无文字按钮可能是图标按钮
                    rect = await btn.bounding_box()
                    if rect and rect["y"] > self.config.viewport_height * 0.6:
                        return btn
        except Exception:
            pass

        return None

    async def start_new_chat(self):
        """开始新对话（避免上下文污染）"""
        try:
            # 查找"新建对话"按钮
            new_chat_selectors = [
                "button:has-text('新建对话')",
                "button:has-text('New chat')",
                "div:has-text('新建对话')",
                "[class*='new-chat']",
                "[class*='newChat']",
            ]
            for selector in new_chat_selectors:
                try:
                    btn = self.page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await self.page.wait_for_timeout(1000)
                        logger.info("已开始新对话")
                        return
                except Exception:
                    continue

            # 回退：直接导航到首页
            await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(1500)
            logger.info("已通过导航开始新对话")
        except Exception as e:
            logger.warning(f"新建对话失败（不影响使用）: {e}")

    async def set_deep_thinking(self, enabled: bool):
        """开启/关闭深度思考模式"""
        if not self.config.use_deep_thinking:
            return
        try:
            # 查找深度思考开关
            thinking_selectors = [
                "div:has-text('深度思考')",
                "button:has-text('深度思考')",
                "div:has-text('DeepThink')",
                "[class*='deep-think']",
                "[class*='thinking']",
            ]
            for selector in thinking_selectors:
                try:
                    elem = self.page.locator(selector).first
                    if await elem.is_visible(timeout=2000):
                        # 检查是否已激活
                        class_attr = await elem.get_attribute("class") or ""
                        if enabled and "active" not in class_attr.lower() and "selected" not in class_attr.lower():
                            await elem.click()
                            await self.page.wait_for_timeout(500)
                            logger.info("已开启深度思考")
                        return
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"设置深度思考失败: {e}")

    async def answer_question(self, question: Question) -> AnswerResult:
        """
        向 DeepSeek 发送题目并获取答案

        Args:
            question: 题目对象

        Returns:
            AnswerResult: 答题结果
        """
        prompt = build_prompt(question)
        logger.info(
            f"题目 {question.index + 1} 发送到 DeepSeek "
            f"(类型: {TYPE_NAMES.get(question.question_type, '未知')}, "
            f"选项数: {len(question.options)})"
        )
        logger.debug(f"提示词:\n{prompt}")

        # 记录当前回复数量
        self._last_response_count = await self._count_responses()

        # 发送问题
        sent = await self._send_message(prompt)
        if not sent:
            return AnswerResult(
                question=question,
                answer_letters=[],
                raw_response="",
                success=False,
                error="无法发送消息到 DeepSeek",
            )

        # 等待回复完成
        raw_response = await self._wait_for_response()

        if not raw_response:
            return AnswerResult(
                question=question,
                answer_letters=[],
                raw_response="",
                success=False,
                error="等待 DeepSeek 回复超时",
            )

        logger.debug(f"题目 {question.index + 1} 原始回复:\n{raw_response[:200]}...")

        # 解析答案
        answer_letters, reasoning = parse_response(raw_response, question)
        if answer_letters:
            logger.info(f"题目 {question.index + 1} 答案: {answer_letters}")
            return AnswerResult(
                question=question,
                answer_letters=answer_letters,
                raw_response=raw_response,
                reasoning=reasoning,
                success=True,
            )

        logger.warning(f"题目 {question.index + 1} 无法解析答案")
        return AnswerResult(
            question=question,
            answer_letters=[],
            raw_response=raw_response,
            reasoning="",
            success=False,
            error="无法解析答案",
        )

    async def answer_questions(self, questions: List[Question]) -> List[AnswerResult]:
        """批量回答题目"""
        results = []
        total = len(questions)
        for i, q in enumerate(questions):
            logger.info(f"正在回答第 {i + 1}/{total} 题...")
            result = await self.answer_question(q)
            results.append(result)
            # 题目间延迟
            if i < total - 1:
                await self.page.wait_for_timeout(int(self.config.question_interval * 1000))
        return results

    async def _send_message(self, message: str) -> bool:
        """向 DeepSeek 发送消息"""
        # 查找输入框
        input_elem = await self._find_input_element()
        if not input_elem:
            logger.error("未找到 DeepSeek 输入框")
            return False

        try:
            # 清空并输入
            await input_elem.click()
            await self.page.wait_for_timeout(300)

            # 使用 fill 或 type 输入
            tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
            if tag == "textarea":
                await input_elem.fill("")
                await input_elem.type(message, delay=10)
            else:
                # contenteditable div
                await input_elem.click()
                await self.page.keyboard.type(message, delay=10)

            await self.page.wait_for_timeout(500)

            # 发送：优先按 Enter，其次点击发送按钮
            if self.config.send_with_enter:
                await self.page.keyboard.press("Enter")
                logger.info("已通过 Enter 发送消息")
                return True

            # 尝试点击发送按钮
            send_btn = await self._find_send_button()
            if send_btn:
                await send_btn.click()
                logger.info("已通过点击发送按钮发送消息")
                return True

            # 回退：按 Enter
            await self.page.keyboard.press("Enter")
            logger.info("已通过 Enter 发送消息（回退）")
            return True

        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    async def _count_responses(self) -> int:
        """统计当前页面上的回复数量"""
        try:
            # DeepSeek 的回复通常在特定容器中
            selectors = [
                "[class*='message-content']",
                "[class*='answer']",
                "[class*='response']",
                "[class*='markdown']",
                "div[data-role='assistant']",
            ]
            for selector in selectors:
                count = await self.page.locator(selector).count()
                if count > 0:
                    return count
        except Exception:
            pass
        return 0

    async def _wait_for_response(self, timeout: int = 120) -> str:
        """
        等待 DeepSeek 回复完成并提取回复文本

        策略：
        1. 等待输入框重新可用（回复完成后输入框会重新激活）
        2. 等待加载/打字指示器消失
        3. 提取最新的回复文本
        """
        logger.info("等待 DeepSeek 回复...")

        start_time = time.time()
        poll_interval = 1.0  # 轮询间隔（秒）

        # 阶段1：等待回复开始出现
        stage1_timeout = 15
        while time.time() - start_time < stage1_timeout:
            current_count = await self._count_responses()
            if current_count > self._last_response_count:
                logger.debug("检测到新回复开始生成")
                break
            await self.page.wait_for_timeout(int(poll_interval * 1000))
        else:
            logger.warning("未检测到回复开始，尝试直接等待完成...")

        # 阶段2：等待回复完成（加载指示器消失 + 内容稳定）
        last_text = ""
        stable_count = 0
        required_stable = 3  # 连续3次内容不变视为完成

        while time.time() - start_time < timeout:
            # 检查是否还在生成
            is_generating = await self._is_still_generating()

            # 获取当前最新回复文本
            current_text = await self._extract_latest_response()

            if current_text and current_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                last_text = current_text

            # 内容稳定且不再生成
            if stable_count >= required_stable and not is_generating:
                logger.info(f"DeepSeek 回复完成（耗时 {time.time() - start_time:.1f}s）")
                return current_text or last_text

            await self.page.wait_for_timeout(int(poll_interval * 1000))

        logger.warning(f"等待回复超时（{timeout}s），返回最后获取到的内容")
        return last_text

    async def _is_still_generating(self) -> bool:
        """检测 DeepSeek 是否仍在生成回复"""
        try:
            # 检查加载/打字指示器
            loading_selectors = [
                "[class*='loading']",
                "[class*='typing']",
                "[class*='generating']",
                "[class*='streaming']",
                "div[class*='cursor']",
                "span[class*='blink']",
                # DeepSeek 特有的停止按钮（生成中会显示）
                "div[role='button']:has(svg)",
            ]

            for selector in loading_selectors:
                try:
                    elem = self.page.locator(selector).first
                    if await elem.is_visible(timeout=500):
                        # 进一步验证是否是加载指示器
                        class_attr = await elem.get_attribute("class") or ""
                        if any(kw in class_attr.lower() for kw in ["load", "type", "generat", "stream", "blink", "cursor"]):
                            return True
                except Exception:
                    continue

            # 检查发送按钮是否禁用（生成中通常会禁用）
            send_btn = await self._find_send_button()
            if send_btn:
                is_disabled = await send_btn.get_attribute("disabled")
                class_attr = await send_btn.get_attribute("class") or ""
                if is_disabled or "disabled" in class_attr.lower():
                    return True

        except Exception:
            pass

        return False

    async def _extract_latest_response(self) -> str:
        """提取最新的回复文本"""
        # 策略1：查找 AI 回复容器（通常有特定标记）
        response_selectors = [
            # DeepSeek 常见回复容器
            "[class*='message-content']:last-child",
            "[class*='answer']:last-child",
            "[class*='response']:last-child",
            "[class*='markdown']:last-of-type",
            "div[data-role='assistant']",
            ".prose:last-child",
            # 通用：最后一个包含较多文本的块
            "[class*='content']:last-child",
        ]

        for selector in response_selectors:
            try:
                elements = self.page.locator(selector)
                count = await elements.count()
                if count > 0:
                    # 取最后一个
                    text = await elements.nth(count - 1).inner_text()
                    text = text.strip()
                    if text and len(text) > 5:
                        # 过滤掉用户消息（通常较短或包含特定标记）
                        if not text.startswith("【") and len(text) > 20:
                            return text
            except Exception:
                continue

        # 策略2：JavaScript 提取所有文本块，取最后一个 AI 回复
        try:
            js_code = """
            () => {
                // 查找所有可能的消息容器
                const selectors = [
                    '[class*="message-content"]',
                    '[class*="answer"]',
                    '[class*="response"]',
                    '[class*="markdown"]',
                    '[data-role="assistant"]',
                    '.prose',
                ];

                let allMessages = [];
                for (const sel of selectors) {
                    const elems = document.querySelectorAll(sel);
                    elems.forEach(el => {
                        const text = el.textContent || el.innerText;
                        if (text && text.trim().length > 10) {
                            allMessages.push({
                                text: text.trim(),
                                rect: el.getBoundingClientRect(),
                            });
                        }
                    });
                }

                if (allMessages.length === 0) return '';

                // 按位置排序（从上到下），取最后一个
                allMessages.sort((a, b) => a.rect.top - b.rect.top);

                // 取最后一个非用户消息（用户消息通常以【开头或较短）
                for (let i = allMessages.length - 1; i >= 0; i--) {
                    const msg = allMessages[i];
                    if (!msg.text.startsWith('【') && msg.text.length > 20) {
                        return msg.text;
                    }
                }

                return allMessages[allMessages.length - 1].text;
            }
            """
            result = await self.page.evaluate(js_code)
            if result:
                return result.strip()
        except Exception as e:
            logger.debug(f"JavaScript 提取回复失败: {e}")

        return ""

    async def test_connection(self) -> bool:
        """测试 DeepSeek 网页是否可访问"""
        try:
            await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(2000)
            title = await self.page.title()
            logger.info(f"DeepSeek 页面标题: {title}")

            # 检查是否有输入框
            input_elem = await self._find_input_element()
            if input_elem:
                logger.info("DeepSeek 网页可访问且已登录")
                return True
            else:
                logger.info("DeepSeek 网页可访问，但需要登录")
                return True  # 网页可访问，只是需要登录
        except Exception as e:
            logger.error(f"DeepSeek 网页访问失败: {e}")
            return False

    async def close(self):
        """关闭资源"""
        if self._owns_browser and self.browser:
            await self.browser.close()
        elif self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self._owns_browser and self.playwright:
            await self.playwright.stop()
        logger.info("DeepSeek 客户端已关闭")
