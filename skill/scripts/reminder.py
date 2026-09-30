"""Prepare a one-shot Codex reminder request; does not contact MinerU or schedule by itself."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

def plan(expires_on):
    configure_script = (Path(__file__).resolve().parent / 'configure-token.ps1').as_posix()
    expiry = date.fromisoformat(expires_on)
    day = expiry - timedelta(days=1)
    when = datetime(day.year, day.month, day.day, 9, tzinfo=timezone(timedelta(hours=8)))
    return {'name': '更换 MinerU Token', 'expires_on': expiry.isoformat(),
            'remind_at': when.isoformat(), 'timezone': 'Asia/Shanghai',
            'frequency': 'once', 'status': 'pending_scheduler',
            'prompt': f'提醒用户：MinerU Token 明天到期。请在 MinerU 官方页面创建新 Token，然后在本机 PowerShell 执行 powershell -NoProfile -ExecutionPolicy Bypass -File "{configure_script}"。不要把 Token 发到聊天。更换后告诉 Codex 新到期日期，以更新下一次到期前一天的单次提醒。不要调用 MinerU API，不要读取或解密 Token。提醒一次后结束。'}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--expires-on', required=True, help='Actual MinerU expiry date YYYY-MM-DD')
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    obj = plan(a.expires_on)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(obj,ensure_ascii=False,indent=2))
