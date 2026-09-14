#!/usr/bin/env python3
"""Compatibility command: execute the caller's loaded Skill, never another copy."""

import os
from pathlib import Path
import sys

entry = os.environ.get("AGENT_DELEGATION_ENTRY", "")
path = Path(entry)
if not path.is_absolute() or not path.is_file() or path.resolve() == Path(__file__).resolve():
    sys.exit("Use the loaded agent-delegation Skill's scripts/agent_delegate.py, or set "
             "AGENT_DELEGATION_ENTRY to that absolute path. No shared Skill copy is installed.")
os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])
