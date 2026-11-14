"""Explicit submission for both mini experiment arms, without Compass maintenance."""

from __future__ import annotations

import re

from minisweagent.agents.interactive import InteractiveAgent
from minisweagent.exceptions import Submitted

_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_SUBMIT = re.compile(rf"\s*echo[ \t]+(?:{_MARKER}|'{_MARKER}'|\"{_MARKER}\")\s*")


class ExplicitSubmissionEnvironment:
    """Keep sentinel text from grep/cat/test output from ending the mini loop.

    mini 2.4's environments raise Submitted before returning output. That exception
    retains the lines after the sentinel, but strips leading whitespace. Restore
    the normalized sentinel line and those retained lines without rerunning the
    command. Unknown exception shapes are left to the host.
    """

    def __init__(self, environment):
        self._environment = environment

    def __getattr__(self, name):
        return getattr(self._environment, name)

    def execute(self, action, *args, **kwargs):
        try:
            return self._environment.execute(action, *args, **kwargs)
        except Submitted as error:
            if _SUBMIT.fullmatch(action.get("command", "")):
                raise
            if len(error.messages) != 1:
                raise
            message = error.messages[0]
            extra = message.get("extra", {})
            submission = extra.get("submission")
            if message.get("role") != "exit" or extra.get("exit_status") != "Submitted" or not isinstance(submission, str):
                raise
            return {
                "returncode": 0,
                "output": f"{_MARKER}\n{submission}",
                "exception_info": "",
            }


class SubmissionSafeMiniAgent(InteractiveAgent):
    """Baseline scaffold: only explicit standalone echo commands can submit."""

    def __init__(self, model, env, **kwargs):
        super().__init__(model, ExplicitSubmissionEnvironment(env), **kwargs)
