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
    """

    DEEPSEEK_URL = "https://chat.deepseek.com/"

    def __init__(self, config: DeepSeekWebConfig):
        self.config = config
        self.playwright = None
        self.browser = None
        self._owns_browser = False
        self.page = None
        self._last_response_count = 0
        self._question_count = 0

    async def init_standalone(self):
        """独立启动浏览器"""
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
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        await self.page.set_viewport_size({
            "width": self.config.viewport_width,
            "height": self.config.viewport_height,
        })

    async def navigate_and_login(self) -> bool:
        """导航到 DeepSeek 并等待用户登录"""
        await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=30000)
        logger.info(f"已打开 DeepSeek: {self.DEEPSEEK_URL}")
        await self.page.wait_for_timeout(3000)

        # 检查是否已登录（查找输入框）
        input_found = await self._find_input_element()
        if not input_found:
            logger.info("DeepSeek 需要登录，请在浏览器中完成登录")
            await self._notify_and_wait(
                "请在 DeepSeek 页面完成登录（手机号/邮箱/微信扫码）"
            )
            input_found = await self._find_input_element()
            if not input_found:
                logger.error("登录后仍未找到输入框")
                return False

        logger.info("DeepSeek 已就绪")
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
        """查找输入框元素"""
        # DeepSeek 使用 textarea 或 contenteditable div
        selectors = [
            "textarea",
            "div[contenteditable='true']",
            "#chat-input",
            "div[role='textbox']",
        ]
        for selector in selectors:
            try:
                elem = self.page.locator(selector).first
                if await elem.is_visible(timeout=2000):
                    logger.debug(f"找到输入框: {selector}")
                    return elem
            except Exception:
                continue
        return None

    async def start_new_chat(self):
        """开始新对话，避免上下文污染"""
        try:
            # 方式1：点击"新建对话"按钮
            new_chat_selectors = [
                "button:has-text('新建对话')",
                "div[role='button']:has-text('新建对话')",
                "a:has-text('新建对话')",
                "button:has-text('New chat')",
                "[class*='new-chat']",
                "[class*='newChat']",
            ]
            for selector in new_chat_selectors:
                try:
                    btn = self.page.locator(selector).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await self.page.wait_for_timeout(1000)
                        logger.info("已开始新对话")
                        return
                except Exception:
                    continue

            # 方式2：直接导航到首页
            await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(2000)
            logger.info("已通过导航开始新对话")
        except Exception as e:
            logger.warning(f"新建对话失败: {e}")

    async def answer_question(self, question: Question) -> AnswerResult:
        """向 DeepSeek 发送题目并获取答案"""
        self._question_count += 1
        prompt = build_prompt(question)
        logger.info(
            f"题目 {question.index + 1} 发送到 DeepSeek "
            f"(类型: {TYPE_NAMES.get(question.question_type, '未知')}, "
            f"选项数: {len(question.options)})"
        )

        # 每题开始新对话，避免上下文干扰
        if self._question_count > 1:
            await self.start_new_chat()

        # 记录发送前的回复数量
        self._last_response_count = await self._count_responses()

        # 发送问题
        sent = await self._send_message(prompt)
        if not sent:
            return AnswerResult(
                question=question, answer_letters=[], raw_response="",
                success=False, error="无法发送消息到 DeepSeek",
            )

        # 等待回复完成
        raw_response = await self._wait_for_response()

        if not raw_response:
            return AnswerResult(
                question=question, answer_letters=[], raw_response="",
                success=False, error="等待 DeepSeek 回复超时",
            )

        logger.debug(f"题目 {question.index + 1} 回复（前200字）:\n{raw_response[:200]}")

        # 解析答案
        answer_letters, reasoning = parse_response(raw_response, question)
        if answer_letters:
            logger.info(f"题目 {question.index + 1} 答案: {answer_letters}")
            return AnswerResult(
                question=question, answer_letters=answer_letters,
                raw_response=raw_response, reasoning=reasoning, success=True,
            )

        logger.warning(f"题目 {question.index + 1} 无法解析答案，原始回复前300字: {raw_response[:300]}")
        return AnswerResult(
            question=question, answer_letters=[], raw_response=raw_response,
            reasoning="", success=False, error="无法解析答案",
        )

    async def _send_message(self, message: str) -> bool:
        """向 DeepSeek 发送消息"""
        input_elem = await self._find_input_element()
        if not input_elem:
            logger.error("未找到 DeepSeek 输入框")
            return False

        try:
            await input_elem.click()
            await self.page.wait_for_timeout(300)

            # 判断元素类型并输入
            tag = await input_elem.evaluate("el => el.tagName.toLowerCase()")
            if tag == "textarea":
                # 用 fill 快速填入，比 type 逐字输入快得多
                await input_elem.fill(message)
            else:
                # contenteditable div - 用 JavaScript 设置内容
                await input_elem.evaluate("""
                    (el, text) => {
                        el.focus();
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                """, message)

            await self.page.wait_for_timeout(500)

            # 发送：优先按 Enter
            if self.config.send_with_enter:
                await self.page.keyboard.press("Enter")
                await self.page.wait_for_timeout(500)
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

    async def _find_send_button(self):
        """查找发送按钮"""
        # DeepSeek 的发送按钮通常是一个带 svg 的按钮，位于输入框右下方
        selectors = [
            "button[type='submit']",
            "button[aria-label*='send']",
            "button[aria-label*='发送']",
            "div[role='button']:has(svg)",
            "button:has(svg)",
        ]
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                count = await loc.count()
                for i in range(count):
                    btn = loc.nth(i)
                    if await btn.is_visible(timeout=1000):
                        # 检查是否在页面下半部分（发送按钮通常在底部）
                        box = await btn.bounding_box()
                        if box and box["y"] > self.config.viewport_height * 0.5:
                            return btn
            except Exception:
                continue
        return None

    async def _count_responses(self) -> int:
        """统计当前页面上的 AI 回复数量"""
        try:
            # DeepSeek 的对话区域，AI 回复通常在特定的容器中
            # 尝试多种选择器
            selectors = [
                "[class*='message'] [class*='content']",
                "[class*='chat-message']",
                "[class*='conversation'] [class*='content']",
                "div[class*='markdown']",
                "div[class*='prose']",
            ]
            for selector in selectors:
                count = await self.page.locator(selector).count()
                if count > 0:
                    return count
        except Exception:
            pass
        return 0

    async def _wait_for_response(self, timeout: int = 120) -> str:
        """等待 DeepSeek 回复完成并提取回复文本"""
        logger.info("等待 DeepSeek 回复...")

        start_time = time.time()
        poll_interval = 1.5

        # 阶段1：等待回复开始出现（最多等15秒）
        stage1_timeout = 15
        while time.time() - start_time < stage1_timeout:
            current_count = await self._count_responses()
            if current_count > self._last_response_count:
                logger.debug("检测到新回复开始生成")
                break
            await self.page.wait_for_timeout(int(poll_interval * 1000))
        else:
            logger.warning("未检测到回复开始，尝试直接等待完成...")

        # 阶段2：等待回复完成（内容稳定 + 停止按钮消失）
        last_text = ""
        stable_count = 0
        required_stable = 3  # 连续3次内容不变视为完成
        poll_count = 0

        while time.time() - start_time < timeout:
            is_generating = await self._is_still_generating()
            current_text = await self._extract_latest_response()
            poll_count += 1

            if current_text and current_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                last_text = current_text

            # 每3次轮询记录一次提取到的内容
            if poll_count % 3 == 0 and current_text:
                logger.debug(f"轮询中 ({poll_count}次), 当前文本: {current_text[:80]}...")

            # 内容稳定且不再生成
            if stable_count >= required_stable and not is_generating and current_text:
                elapsed = time.time() - start_time
                logger.info(f"DeepSeek 回复完成（耗时 {elapsed:.1f}s）")
                logger.debug(f"最终回复内容（前200字）:\n{current_text[:200]}")
                return current_text

            await self.page.wait_for_timeout(int(poll_interval * 1000))

        logger.warning(f"等待回复超时（{timeout}s），返回最后内容")
        return last_text

    async def _is_still_generating(self) -> bool:
        """检测 DeepSeek 是否仍在生成回复"""
        try:
            # 策略1：查找停止生成按钮（DeepSeek 生成中会显示一个停止按钮）
            stop_selectors = [
                "div[role='button'][class*='stop']",
                "button[class*='stop']",
                "div[class*='stop-generating']",
                "[class*='stopBtn']",
                # DeepSeek 的停止按钮可能是一个圆形按钮带方形图标
                "div[role='button']:has(span[class*='square'])",
            ]
            for selector in stop_selectors:
                try:
                    elem = self.page.locator(selector).first
                    if await elem.is_visible(timeout=500):
                        return True
                except Exception:
                    continue

            # 策略2：查找加载/打字指示器
            loading_selectors = [
                "[class*='loading']",
                "[class*='typing']",
                "[class*='generating']",
                "[class*='streaming']",
                "span[class*='blink']",
                "div[class*='cursor-blink']",
                "div[class*='animate-pulse']",
            ]
            for selector in loading_selectors:
                try:
                    elem = self.page.locator(selector).first
                    if await elem.is_visible(timeout=500):
                        return True
                except Exception:
                    continue

            # 策略3：检查输入框是否可编辑（生成中输入框通常不可用）
            input_elem = await self._find_input_element()
            if input_elem:
                is_disabled = await input_elem.get_attribute("disabled")
                if is_disabled:
                    return True
                readonly = await input_elem.get_attribute("readonly")
                if readonly:
                    return True

        except Exception:
            pass

        return False

    # DeepSeek 页面上的 UI 文本黑名单（这些不是 AI 回复）
    UI_BLACKLIST_PATTERNS = [
        "内容由 AI 生成", "请仔细甄别", "深度思考", "智能搜索",
        "联网搜索", "R1", "DeepThink", "Web Search",
        "重新生成", "复制", "点赞", "踩", "分享",
        "发送消息", "Send a message",
        "新建对话", "New chat",
        "快速模式", "联网搜索", "上传文件",
        "单选题", "多选题", "判断题", "填空题",
        "这是一道", "请选出正确答案", "请只输出答案",
        "输出格式", "注意：必须严格按照",
    ]

    def _is_ui_text(self, text: str) -> bool:
        """判断文本是否是 DeepSeek 页面的 UI 文本而非 AI 回复"""
        # 包含【答案】的一定是有效回复
        if "【答案】" in text:
            return False
        # 太短的不可能是有效回复
        if len(text) < 10:
            return True
        # 检查黑名单
        for pattern in self.UI_BLACKLIST_PATTERNS:
            if pattern in text:
                return True
        # 纯 UI 按钮文字组合
        ui_only = True
        for pattern in self.UI_BLACKLIST_PATTERNS:
            text_without = text.replace(pattern, "").strip()
            if len(text_without) > 20:
                ui_only = False
                break
        if ui_only and len(text) < 200:
            return True
        # 重复文本（如 "真空干燥单选题快速模式真空干燥单选题快速模式"）
        if len(text) < 100:
            half = len(text) // 2
            if half > 5 and text[:half] == text[half:half*2]:
                return True
        return False

    async def _extract_latest_response(self) -> str:
        """提取最新的 AI 回复文本"""
        # 策略1：JavaScript 智能提取，严格过滤 UI 文本和用户消息
        try:
            js_code = """
            () => {
                // UI 黑名单关键词
                const uiBlacklist = [
                    '内容由 AI 生成', '请仔细甄别', '深度思考', '智能搜索',
                    '联网搜索', '重新生成', '复制', '点赞', '踩',
                    '发送消息', '新建对话', 'DeepThink', 'Web Search',
                    '快速模式', '上传文件',
                    '单选题', '多选题', '判断题', '填空题',
                    '这是一道', '请选出正确答案', '请只输出答案',
                    '输出格式', '注意：必须严格按照',
                ];

                function isUiText(text) {
                    if (text.includes('【答案】') || text.includes('【解析】')) return false;
                    if (text.length < 10) return true;
                    for (const kw of uiBlacklist) {
                        if (text.includes(kw)) return true;
                    }
                    // 去掉所有 UI 关键词后剩余太短
                    let nonUi = text;
                    for (const kw of uiBlacklist) {
                        nonUi = nonUi.split(kw).join('');
                    }
                    nonUi = nonUi.trim();
                    if (nonUi.length < 20 && text.length < 200) return true;
                    // 重复文本检测
                    if (text.length < 100) {
                        const half = Math.floor(text.length / 2);
                        if (half > 5 && text.substring(0, half) === text.substring(half, half * 2)) return true;
                    }
                    return false;
                }

                function isUserMessage(text) {
                    // 用户消息包含提示词特征
                    if (text.includes('这是一道') && text.includes('题目：')) return true;
                    if (text.includes('请只输出答案') || text.includes('输出格式')) return true;
                    if (text.includes('请选出正确答案')) return true;
                    return false;
                }

                // 查找所有可能包含 AI 回复的元素
                // 优先级：markdown 渲染区 > 消息内容区 > 一般 div
                const selectors = [
                    '[class*="markdown"]',
                    '[class*="message"] [class*="content"]',
                    '[class*="response"]',
                    '[class*="answer"]',
                    '[class*="prose"]',
                    '[class*="bot"]',
                    'div[class*="content"]',
                ];

                const candidates = [];
                for (const selector of selectors) {
                    const elements = document.querySelectorAll(selector);
                    for (const el of elements) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 100 || rect.height < 10) continue;

                        // 只取叶子级别的文本（避免父容器包含用户消息）
                        const fullText = (el.textContent || '').trim();
                        if (fullText.length < 5) continue;

                        // 排除用户消息
                        if (isUserMessage(fullText)) continue;
                        // 排除 UI 文本
                        if (isUiText(fullText)) continue;
                        // 排除侧边栏
                        if (rect.width < 300) continue;
                        // 排除输入框区域
                        if (rect.bottom > window.innerHeight * 0.9) continue;

                        candidates.push({
                            text: fullText,
                            top: rect.top,
                            bottom: rect.bottom,
                            height: rect.height,
                            width: rect.width,
                            area: rect.width * rect.height,
                        });
                    }
                }

                if (candidates.length === 0) return '';

                // 按面积排序，大的优先（AI 回复通常面积最大）
                candidates.sort((a, b) => b.area - a.area);

                // 优先找包含【答案】的
                for (const c of candidates) {
                    if (c.text.includes('【答案】')) {
                        return c.text;
                    }
                }

                // 取面积最大的有效文本
                // 但要排除太大的容器（可能包含多个消息）
                const filtered = candidates.filter(c => c.height < 2000);
                if (filtered.length > 0) {
                    // 在合理大小的容器中，取最靠下的（最新回复）
                    filtered.sort((a, b) => b.top - a.top);
                    return filtered[0].text;
                }

                return candidates[0].text;
            }
            """
            result = await self.page.evaluate(js_code)
            if result and len(result) > 5:
                result = result.strip()
                # 二次验证
                if not self._is_ui_text(result):
                    logger.debug(f"JS 提取回复成功（{len(result)} 字）: {result[:80]}")
                    return result
                else:
                    logger.debug(f"提取到 UI 文本，跳过: {result[:80]}")
            elif result:
                logger.debug(f"提取回复太短: '{result}'")
        except Exception as e:
            logger.debug(f"JavaScript 提取回复失败: {e}")

        # 策略2：通过 CSS 选择器查找
        response_selectors = [
            "[class*='markdown']:last-of-type",
            "[class*='message']:last-child [class*='content']",
            "[class*='answer']:last-child",
            "[class*='response']:last-child",
            "[class*='prose']:last-of-type",
            "[class*='bot']:last-child",
            "div[class*='content']:last-of-type",
        ]
        for selector in response_selectors:
            try:
                elements = self.page.locator(selector)
                count = await elements.count()
                if count > 0:
                    text = await elements.nth(count - 1).inner_text()
                    text = text.strip()
                    if text and len(text) > 10:
                        if not self._is_ui_text(text):
                            if not ("这是一道" in text and "题目：" in text):
                                logger.debug(f"CSS 选择器 '{selector}' 提取成功: {text[:80]}")
                                return text
            except Exception:
                continue

        return ""

    async def test_connection(self) -> bool:
        """测试 DeepSeek 网页是否可访问"""
        try:
            await self.page.goto(self.DEEPSEEK_URL, wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(2000)
            title = await self.page.title()
            logger.info(f"DeepSeek 页面标题: {title}")
            return True
        except Exception as e:
            logger.error(f"DeepSeek 网页访问失败: {e}")
            return False

    async def close(self):
        """关闭资源"""
        if self._owns_browser and self.browser:
            await self.browser.close()
        if self._owns_browser and self.playwright:
            await self.playwright.stop()
        logger.info("DeepSeek 客户端已关闭")
