# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

---

## 🔐 Git 推送排障速查（2026-10-26 实测补充）

### 三个高频坑

| 症状 | 真实原因 | 解法 |
|------|---------|------|
| "用了 SSH 但还是 443 超时" | 全局 `url.https://github.com/.insteadof` 把 SSH 重写回 HTTPS | `git config --global --unset-all 'url.https://github.com/.insteadof'`（**必须用完整 key**，含斜杠的 section 名 `--unset-all` 无效） |
| push 成功但远端没变 | 推了 `main` 而远端是 `master` | `git ls-remote origin` 先确认分支名 |
| `secret scanning` 拦截，提示某 commit | 密钥在**历史 commit 的 tree** 里，后续 `git rm --cached` 无效 | 必须 rebase 重写该 commit |

### 清除历史密钥的标准流程
```bash
cp GIT_CREDENTIALS.md /root/.openclaw/xxx.local.md && chmod 600 /root/.openclaw/xxx.local.md
echo "GIT_CREDENTIALS.md" >> .gitignore
git rm --cached GIT_CREDENTIALS.md
# 重写引入密钥的那个 commit
git rebase --onto <bad_commit>^ <bad_commit> master
# 冲突逐个处理
git checkout --theirs <file> && git add <file> && git -c core.editor=true rebase --continue
# 验证历史已干净
for c in $(git rev-list origin/master..HEAD); do
  git show $c:GIT_CREDENTIALS.md >/dev/null 2>&1 && echo "STILL PRESENT: $c"
done
```

### 建议：Git 远端统一用 SSH
```bash
git remote set-url origin git@github.com:nqge/<repo>.git
git ls-remote origin   # 验证
```

### 经验法则
> 「Everything up-to-date」有时出现在 push 断连之后，是**误导性输出**——以 `git rev-list --count origin/master..HEAD` 为准。

---

_最后更新: 2026-10-26 01:25_
_维护者: 小红🌸_
