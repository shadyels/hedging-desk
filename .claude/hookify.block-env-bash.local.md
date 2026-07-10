---
name: block-env-bash
enabled: true
event: bash
action: block
pattern: (?:cat|less|more|head|tail|vim|vi|nano|emacs|code|open|bat|sed|awk|grep|tee|cp|mv|>>?)\s+[^\n]*\.env(?:\.[\w.-]+)?\b
---

`.env` files are never read or written via shell commands by an agent. They
hold local secrets/config.

If you need to check whether a variable is set, ask the user to confirm
rather than reading the file's contents.
