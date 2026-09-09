# Skills

Portable, personal Copilot CLI skills. Each subfolder is a self-contained skill with a
`SKILL.md` (frontmatter + instructions) and any supporting scripts.

This repo is **public** and serves GitHub Pages. Nothing here may contain personal data:

- No email addresses, sender lists, contact names, or mailbox contents
- No tokens, secrets, client secrets, or cookies
- All per-user configuration lives outside the repo (see each skill's SKILL.md)

Committed files are code and templates only.

## Installing a skill

Copilot CLI discovers user-scope skills in `~/.copilot/skills/`. Symlink (or copy) a skill
folder there:

```powershell
# from the repo root
$src = "$PWD\skills\email-hygiene"
$dst = "$env:USERPROFILE\.copilot\skills\email-hygiene"
New-Item -ItemType SymbolicLink -Path $dst -Target $src   # requires Developer Mode or admin
# fallback if symlinks are unavailable:
# Copy-Item $src $dst -Recurse -Force
```

## Skills

| Skill | Purpose |
|---|---|
| [`email-hygiene`](email-hygiene/) | Scheduled cleanup of a personal Outlook.com mailbox — noise triage, repetitive-sender detection, and unsubscribe recommendations. |
