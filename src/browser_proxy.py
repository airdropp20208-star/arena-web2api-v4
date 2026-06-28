"""
Browser proxy — dùng browser để gọi Arena API (vì reCAPTCHA chỉ hoạt động trong browser context).

Thay vì server gọi Arena API trực tiếp, server sẽ:
1. Gửi request đến browser (agent-browser eval)
2. Browser gọi Arena API (có reCAPTCHA hợp lệ)
3. Browser trả response về server
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from src.logger import setup_logger

logger = setup_logger(__name__)


async def stream_via_browser(
    payload: dict,
    *,
    timeout: float = 60,
) -> AsyncIterator[str]:
    """
    Gọi Arena API qua browser và yield SSE text chunks.
    """
    # Escape payload for JavaScript
    payload_json = json.dumps(payload)

    js_code = f"""
    (async () => {{
        const payload = {payload_json};
        
        // Get reCAPTCHA token
        const recaptchaToken = await grecaptcha.enterprise.execute(
            '6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0',
            {{action: 'submit'}}
        );
        payload.recaptchaV3Token = recaptchaToken;
        
        const resp = await fetch('/nextjs-api/stream/create-evaluation', {{
            method: 'POST',
            credentials: 'include',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
        }});
        
        if (!resp.ok) {{
            const text = await resp.text();
            return JSON.stringify({{error: true, status: resp.status, body: text.substring(0, 500)}});
        }}
        
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let result = '';
        
        while (true) {{
            const {{done, value}} = await reader.read();
            if (done) break;
            result += decoder.decode(value, {{stream: true}});
        }}
        
        return JSON.stringify({{error: false, body: result}});
    }})()
    """

    try:
        proc = await asyncio.create_subprocess_exec(
            "agent-browser", "eval", js_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )

        if proc.returncode != 0:
            logger.warning(f"Browser proxy error: {stderr.decode()[:200]}")
            return

        if not stdout:
            logger.warning("Browser proxy: empty response")
            return

        result = json.loads(stdout.decode().strip())

        # agent-browser eval wraps result in JSON, so it could be a string
        if isinstance(result, str):
            # Try to parse the string as JSON
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                logger.warning(f"Browser proxy:无法 parse result: {result[:200]}")
                return

        if not isinstance(result, dict):
            logger.warning(f"Browser proxy: unexpected result type: {type(result)}")
            return

        if result.get("error"):
            logger.warning(
                f"Browser proxy API error: {result.get('status')} - {result.get('body', '')[:200]}"
            )
            return

        body = result.get("body", "")
        if body:
            yield body

    except asyncio.TimeoutError:
        logger.warning(f"Browser proxy timeout after {timeout}s")
    except Exception as e:
        logger.warning(f"Browser proxy exception: {e}")
