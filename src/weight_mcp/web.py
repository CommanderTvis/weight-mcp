"""The human-facing OAuth page: username + password, nothing else."""

from html import escape

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>weight-mcp · sign in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
    font: 15px/1.5 system-ui, sans-serif; background: #0f1115; color: #e6e8eb; }}
  form {{ background: #181b21; border: 1px solid #262a31; border-radius: 14px;
    padding: 28px; width: 320px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p {{ color: #9aa3ad; margin: 0 0 20px; font-size: 13px; }}
  input {{ width: 100%; padding: 10px 12px; border-radius: 8px; box-sizing: border-box;
    border: 1px solid #2d323b; background: #0f1115; color: #e6e8eb; font-size: 15px; }}
  input + input {{ margin-top: 10px; }}
  button {{ width: 100%; margin-top: 14px; padding: 10px; border: 0; border-radius: 8px;
    background: #4f9dff; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; }}
  .error {{ color: #ff6b6b; font-size: 13px; margin-top: 12px; }}
</style></head>
<body>
  <form method="post" action="{action}">
    <h1>weight-mcp</h1>
    <p>{subtitle}</p>
    <input type="hidden" name="txn" value="{txn}">
    <input type="text" name="username" placeholder="Username" autofocus required
           autocapitalize="none" autocorrect="off" spellcheck="false" autocomplete="username">
    <input type="password" name="password" placeholder="Password" required
           autocomplete="current-password">
    <button type="submit">Connect</button>
    {error}
  </form>
</body></html>"""


def login_page(
    action: str,
    *,
    txn: str = "",
    subtitle: str = "Sign in to connect this server to Claude.",
    error: str | None = None,
) -> str:
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    return _PAGE.format(
        action=escape(action),
        txn=escape(txn),
        subtitle=escape(subtitle),
        error=error_html,
    )
