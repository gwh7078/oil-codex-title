"""定位所属插件，调用共享标题与轻量归档程序，避免 Skill 复制实现。"""
from pathlib import Path
import runpy
import sys

scripts = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(scripts))

trigger_commands = {
    "trigger-configure": "configure",
    "trigger-status": "status",
    "archive-scan": "archive-scan",
}
if len(sys.argv) > 1 and sys.argv[1] in trigger_commands:
    sys.argv[1] = trigger_commands[sys.argv[1]]
    target = scripts / "oil_codex_title_trigger.py"
else:
    target = scripts / "oil_codex_title.py"

runpy.run_path(str(target), run_name="__main__")
