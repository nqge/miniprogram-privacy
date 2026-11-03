#!/usr/bin/env python3
r"""每日学习/反思 小节的长度判据自检 —— 2026-11-03 新增

背景（一个真实的「断言装死」）：
  `trim_learning_section.py` 和 healthcheck 的【F】组都用同一条正则去量小节长度：

      r'^## (📚 每日学习|🪞 每日反思).*?\n(.*?)(?=\n#{1,6} |\Z)'

  终止符 `#{1,6}` 会在**第一个 `###` 子标题**处截断。
  而「每日反思（三问）」天然长这样：

      ## 🪞 每日反思（三问）
      ### 1. 今天是否浪费了用户的时间？
      ### 2. 在哪个决策点提供了关键异议？
      ### 3. 从用户的决策偏好中学到了什么？

  → 只量到第一个 `###` 之前的引言块 = **19 汉字**（真实 844）
  → 报「偏短/合规」，而实际超长一倍 → **这个断言在反思小节上基本没生效**

  而且 healthcheck 的**注释**写的是「小节在下一个任意级标题处结束」——
  注释描述的意图和代码实现**相反**，注释反而是对的。

修法：**双终止符**
  ① `^## `         —— 下一个同级小节
  ② `^### ` + 日期 —— 「另一个顶层记录」伪装的子标题
  （10-27 那种「学习小节下挂了 30 多条 ### 心跳记录」必须在 ② 截断，
    否则会把别人的内容算进来 → 报 12095 汉字）

⚠️ 本测试的三个铁律（源自 E52 / E53）：
  1. **测真身代码** —— 直接 import `trim_learning_section`，不抄一份正则
  2. **必须做故障注入** —— 正则的盲区只能靠「构造出它测不出来的样本」抓
  3. **判据本身要保守** —— 量不准时不能报「合规」

跑法：python3 scripts/test_section_length_checker.py
退出码 0 = 全通过
"""
import os
import re
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WS, "scripts"))

import trim_learning_section as tls  # ← 真身，不是抄版


def measure(text):
    """用真身的 SEC_RE 量一段文本里的小节汉字数。"""
    m = tls.SEC_RE.search(text)
    if not m:
        return None, None
    return tls.zh_count(m.group(2)), m.group(1)


def main():
    cases = []

    # ── 1. 反思小节带 ### 子标题 → 必须量到全部内容 ──
    #    这就是本次的真实 bug：旧正则只量到 19 汉字
    reflect_with_sub = (
        "## 🪞 每日反思（三问）\n"
        "> catch-up run\n"
        "\n"
        "### 1. 今天是否浪费了用户的时间？\n"
        + "额度耗尽那三天系统安静地什么都没发生。" * 20 + "\n"
        "\n"
        "### 2. 在哪个决策点提供了关键异议？\n"
        + "三个模型全四二九备用链是假的。" * 20 + "\n"
        "\n"
        "### 3. 从用户的决策偏好中学到了什么？\n"
        + "内因放手改外因列清楚等回复。" * 20 + "\n"
    )
    n, title = measure(reflect_with_sub)
    real = len(re.findall(r"[一-鿿]", reflect_with_sub.split("### 1.")[1]))
    cases.append(
        (n is not None and n > 200,
         f"反思小节带 ### 子标题时应量到全部内容（实测 {n}，应 >200）")
    )
    cases.append(
        (n is not None and n > real * 0.9,
         f"量到的字数应接近真实内容（量到 {n}，真实约 {real}）")
    )

    # ── 2. 学习小节带日期的 ### 记录 → 必须在那个 ### 处截断 ──
    #    否则会把后面几十条心跳记录的内容全算进来
    learn_with_records = (
        "## 📚 每日学习（2026-10-27）\n"
        "> 本条讲：**被吞掉的报错就是装死的断言**\n"
        + "这是一段学习内容。" * 20 + "\n"
        "\n"
        "### 2026-10-27 07:09 心跳 — 实测耗时没人管\n"
        + "这条是另一条顶层记录，不该被算进来。" * 50 + "\n"
        "\n"
        "### 2026-10-27 08:15 心跳 — 断言量错了东西\n"
        + "这条也是另一条记录。" * 50 + "\n"
    )
    n2, _ = measure(learn_with_records)
    cases.append(
        (n2 is not None and n2 < 400,
         f"学习小节应在上一个带日期的 ### 记录处截断（实测 {n2}，应 <400）")
    )

    # ── 3. 下一个 ## 一定截断 ──
    two_sections = (
        "## 📚 每日学习（2026-11-01）\n"
        + "学习内容。" * 30 + "\n"
        "\n"
        "## 🪞 每日反思（三问）\n"
        + "反思内容。" * 30 + "\n"
    )
    n3, t3 = measure(two_sections)
    cases.append(
        (n3 is not None and n3 < 300,
         f"下一个 ## 应截断（实测 {n3}，应 <300）")
    )
    cases.append(
        (t3 is not None and "每日学习" in t3,
         "应匹配到第一个（每日学习）小节")
    )

    # ── 4. 不带日期的 ### 子标题 → 算本节内部，保留 ──
    #    「### 1. …」「### 📌 待牛哥哥决策」这类不是新记录
    learn_with_todo = (
        "## 📚 每日学习（2026-11-02）\n"
        + "学习正文。" * 20 + "\n"
        "\n"
        "### 📌 待牛哥哥决策\n"
        + "这一段属于本节内部。" * 10 + "\n"
    )
    n4, _ = measure(learn_with_todo)
    base = len(re.findall(r"[一-鿿]", "学习正文。" * 20))
    cases.append(
        (n4 is not None and n4 > base,
         f"不带日期的 ### 子标题应算本节内部（实测 {n4}，基础 {base}）")
    )

    # ── 5. 找不到小节 → 返回 None，不该瞎报合规 ──
    n5, t5 = measure("## 其他小节\n随便写点什么\n")
    cases.append(
        (n5 is None and t5 is None,
         "没有学习/反思小节时应返回 None（不是 0，也不是乱报）")
    )

    # ── 6. 【故障注入】旧正则（`#{1,6}` 终止）必须测不出带子标题的反思 ──
    #     这是本次 bug 的核心：证明新正则真的修好了，不是「本来就对」
    OLD = re.compile(
        r'^## (📚 每日学习|🪞 每日反思).*?\n(.*?)(?=\n#{1,6} |\Z)', re.M | re.S)
    mo = OLD.search(reflect_with_sub)
    old_n = tls.zh_count(mo.group(2)) if mo else None
    cases.append(
        (old_n is not None and old_n < 100,
         f"【注入】旧正则确实测不出来（量到 {old_n}，应 <100）")
    )
    cases.append(
        (n is not None and old_n is not None and n > old_n * 2,
         f"【注入】新正则必须明显优于旧正则（新 {n} vs 旧 {old_n}）")
    )

    # ── 7. 真实回归样本：2026-11-03 反思小节应为 393 字（上限 400）──
    p = os.path.join(WS, "memory", "2026-11-03.md")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            real_txt = f.read()
        n7, t7 = measure(real_txt)
        cases.append(
            (n7 is not None and 100 <= n7 <= 400,
             f"真实样本 2026-11-03 反思小节应在 100-400（实测 {n7}）")
        )
    else:
        cases.append((False, "真实样本 2026-11-03.md 不存在"))

    failed = 0
    for ok, name in cases:
        if ok:
            print(f"✅ PASS  {name}")
        else:
            print(f"❌ FAIL  {name}")
            failed += 1

    print("=" * 60)
    if failed:
        print(f" 结果: {len(cases) - failed}/{len(cases)} 通过，{failed} 个失败")
    else:
        print(f" 结果: {len(cases)}/{len(cases)} 全部通过")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
