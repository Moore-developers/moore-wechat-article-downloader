# Compliance and Safety

## Hard Boundaries

- Do not bypass login, paywalls, deleted content, or platform permission checks.
- Do not create account pools.
- Do not scrape private or unauthorized content.
- Do not store raw cookies, tokens, pass tickets, auth headers, or keys in this repository.
- Do not print credential material in final user-facing responses.
- Do not hide credential-dependent behavior behind a generic "batch download" label.

## User Consent

Known public article URLs can be downloaded as user-provided inputs.

Account-history fetching is different. Before any account-history adapter runs, the agent should explain:

- it needs valid user-owned access context
- it may break if WeChat changes endpoints
- it may be subject to platform rules and copyright limits
- the user must confirm the action

## Copyright

Downloaded articles are for the user's legitimate archive or research. Keep source provenance so later work can be traced back.

Do not output large verbatim copyrighted article bodies in chat.

## Local-First Defaults

- Store data under `~/.moore/wechat-article-downloader/`.
- Use Skill-run local CLI commands instead of a Dashboard.
- Avoid network services unless the user explicitly asks.
- Keep original article files immutable and save derived work separately.
